import datetime
import json
import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Q, F
from django.http import JsonResponse, HttpResponse, HttpResponseRedirect, HttpResponseForbidden
from django.shortcuts import render, get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views import View
from django.views.decorators.http import require_POST

from accounts.models import Partner, Client, ClientRate, PartnerLocation, Department, User
from elph2.utils import safaricom_c2b_payment_init_response as elphways_c2b_payment_init_response, \
    init_safaricom_payment as init_elphways_payment
from orders.forms import DeliveryNoteForm, OrderForm, OrderDropOffForm, PayOrderForm, CostEstimatorForm, \
    ConfirmDropOffForm
from orders.models import Order2, OrderDropOff, ORDER_CHOICES, Invoice, OrderQuote
from orders.utils import partner_order_next_status_change, order_next_stop, get_order_route, client_invoices_summary
from utils.forms import MpesaNumberForm
from utils.geo_utils import calculate_distance
from vehicles.models import VehicleType
from vehicles.utils import get_rate_price

logger = logging.getLogger(__name__)


def is_partner_check(user):
    return Partner.objects.filter(user=user).exists()


def is_client_check(user):
    return Client.objects.filter(user=user).exists()


@login_required(redirect_field_name=None)
@user_passes_test(is_partner_check, redirect_field_name=None)
def partner_orders_view(request):
    orders_q = Order2.objects.filter(Q(vehicle_owner__user=request.user) | Q(driver__user=request.user))
    context = {}

    query = request.GET.get('q')
    filtr = request.GET.get('filter')

    if filtr:
        from_date = filtr.split('|')[0]
        if from_date:
            from_date = datetime.datetime.strptime(
                from_date, '%Y-%m-%d %H:%M'
            ).replace(tzinfo=datetime.timezone.utc)
            orders_q = orders_q.filter(created_at__gte=from_date)
            context['from_date'] = from_date.strftime('%Y-%m-%d %H:%M')

        to_date = filtr.split('|')[1]
        if to_date:
            to_date = datetime.datetime.strptime(
                to_date, '%Y-%m-%d %H:%M'
            ).replace(tzinfo=datetime.timezone.utc)
            orders_q = orders_q.filter(created_at__lte=to_date)
            context['to_date'] = to_date.strftime('%Y-%m-%d %H:%M')

    if query:
        # Search
        orders_q = orders_q.filter(
            Q(id_2__icontains=query) | Q(vehicle__reg_no__icontains=query) |
            Q(driver__user__full_name__icontains=query)
        )

    orders_q = orders_q.distinct().order_by('-created_at')
    paginator = Paginator(orders_q, 20)  # Show 20 per page.
    page_number = request.GET.get('page')
    context['orders'] = paginator.get_page(page_number)

    return render(request, 'orders/partner_orders.html', context)


class PartnerOrderView(View, LoginRequiredMixin, UserPassesTestMixin):
    redirect_field_name = None

    def test_func(self):
        return is_partner_check(self.request.user)

    def get(self, request, id_2):
        order = Order2.objects.filter(id_2=id_2).annotate(
            delivery_time=F('delivered_at') - F('delivery_started_at')
        ).first()
        if not order:
            # Order not found
            return HttpResponse(status=404)

        partner = self.request.user.partner

        if not (order.vehicle_owner == partner or order.driver == partner):
            # Not allowed
            raise PermissionDenied

        partner_type = 'vehicle_owner' if order.vehicle_owner == partner else 'driver'

        # Get the next stop for this order
        next_stop = order_next_stop(order)

        context = {
            'order': order,
            'next_status_change': partner_order_next_status_change(order.status, partner_type),
            'next_stop': next_stop,
            'order_next_stop': next_stop,  # Add this for compatibility with other templates
            'drop_offs': OrderDropOff.objects.filter(order=order).order_by('id'),
            'pending_drop_offs': OrderDropOff.objects.filter(order=order, status='SCHEDULED').exists(),
            'google_api_key': settings.GOOGLE_FRONTEND_API_KEY,
            'items_summary': order.get_items_summary(),
            'total_items': order.get_total_items(),
        }
        return render(self.request, 'orders/partner_single_order.html', context)

    def post(self, request, id_2):
        order = get_object_or_404(Order2, id_2=id_2)

        # Check if user is authorized to update this order
        is_owner = getattr(self.request.user.partner, 'vehicle_owner',
                           False) and order.vehicle_owner == self.request.user.partner
        is_driver = getattr(self.request.user.partner, 'driver', False) and order.driver == self.request.user.partner

        if not (is_owner or is_driver):
            # Not authorized
            return HttpResponseForbidden()

        intent = self.request.POST['intent']

        if intent == 'change_status':
            status = self.request.POST.get('status')

            # Check if all dropoffs are handled for DELIVERED status
            if status == 'DELIVERED' and not order.all_dropoffs_handled():
                # Cannot mark as delivered yet
                error_message = 'Cannot mark order as delivered.'
                error_detail = 'Please complete all scheduled drop offs before marking as delivered.'

                # Check specifically for skipped dropoffs
                if order.has_skipped_dropoffs():
                    error_message = 'Cannot mark order as delivered while there are skipped dropoffs.'
                    error_detail = 'Please complete all skipped dropoffs before marking the order as delivered.'

                return JsonResponse(
                    data={
                        'message': error_message,
                        'errors': {'status': [error_detail]}
                    },
                    status=400
                )

            if status not in [i[0] for i in ORDER_CHOICES]:
                # Invalid status
                return JsonResponse(
                    data={
                        'message': 'Invalid status.',
                        'errors': {'status': ['Invalid status.']}
                    },
                    status=400
                )

            # Handle delivery notes for DELIVERED status
            if status == 'DELIVERED':
                form = DeliveryNoteForm(data=self.request.POST, files=self.request.FILES, instance=order)
                if not form.is_valid():
                    for key, value in json.loads(form.errors.as_json()).items():
                        for err in value:
                            return JsonResponse(data={'message': err['message']}, status=400)
                
                # Save delivery notes
                form.save()
            else:
                # Change status
                order.status = status

                if status == 'CONFIRMED':
                    order.confirmed_at = timezone.now()
                elif status == 'IN_TRANSIT':
                    order.delivery_started_at = timezone.now()
                elif status == 'DELIVERED':
                    order.delivered_at = timezone.now()

                order.save()

            messages.success(self.request, f'Order status changed to {order.get_status_display()}.')
            return JsonResponse({'callback': 'reload'}, status=200)

        elif intent == 'stop_arrival':
            stop = get_object_or_404(OrderDropOff, pk=self.request.POST['stop'], order=order)
            form = ConfirmDropOffForm(data=self.request.POST, files=self.request.FILES, instance=stop)

            # For complete action, check driver location
            if self.request.POST.get('action') == 'complete' and order.driver:
                latest_location = PartnerLocation.objects.filter(
                    partner=order.driver
                ).order_by('-timestamp').first()

                if latest_location:
                    destination_lat, destination_lng = stop.latlng.split(',')

                    distance = calculate_distance(
                        destination_lat, destination_lng,
                        latest_location.latitude, latest_location.longitude
                    )

                    logger.info(f"Distance for vehicle from drop off : {distance} km")
                    
                    # if distance < 1000:
                    #     # Distance is too far
                    #     return JsonResponse(
                    #         data={
                    #             'message': f'Driver is too far from destination. '
                    #                        f'cannot complete order need to be at least {0.9 * 1000} m '
                    #                        f'from destination you are currently at {int(distance * 1000)} m ',
                    #             'errors': {'delivery_note': [
                    #                 f'Driver is too far from destination. be within {0.9 * 1000} m to complete order']}
                    #         },
                    #         status=400
                    #     )

            if not form.is_valid():
                # Invalid data
                return JsonResponse({'errors': form.errors}, status=400)

            form.save()

            # Customize success message based on action
            action = form.cleaned_data.get('action')
            if action == 'complete':
                messages.success(self.request, f'Drop off completed at {stop.address}.')
            elif action == 'skip':
                messages.success(self.request, f'Drop off skipped at {stop.address}.')
            elif action == 'reschedule':
                messages.success(self.request, f'Drop off at {stop.address} rescheduled.')

            return JsonResponse({'callback': 'reload'}, status=200)

        elif intent == 'resume_stop':
            stop = get_object_or_404(OrderDropOff, pk=self.request.POST['stop'], order=order)

            if stop.status != 'SKIPPED':
                # Not a skipped dropoff
                return JsonResponse(
                    data={
                        'message': 'Cannot resume this dropoff as it is not in skipped status.',
                        'errors': {'stop': ['Only skipped dropoffs can be resumed.']}
                    },
                    status=400
                )

            # Resume the dropoff
            stop.resume()
            messages.success(self.request, f'Resumed dropoff at {stop.address}.')
            return JsonResponse({'callback': 'reload'}, status=200)


@login_required(redirect_field_name="")
@user_passes_test(is_client_check, redirect_field_name="")
def client_orders_view(request):
    client = request.user.client
    organization = request.user.organization

    # If user is part of an organization, show appropriate orders
    if organization:
        if request.user.is_org_admin:
            # Admin sees all organization orders
            client_ids = Client.objects.filter(user__organization=organization).values_list('id', flat=True)
            orders_q = Order2.objects.filter(client_id__in=client_ids)
        elif request.user.department:
            # Department user sees department orders + their own orders
            orders_q = Order2.objects.filter(
                Q(client=client) | 
                Q(department=request.user.department, client__user__organization=organization)
            ).distinct()
        else:
            # Regular user sees only their orders
            orders_q = Order2.objects.filter(client=client)
    else:
        # Original behavior for users not in an organization
        orders_q = Order2.objects.filter(client=client)

    context = {}
    query = request.GET.get('q')
    filtr = request.GET.get('filter')
    department_filter = request.GET.get('department')

    # Department filter
    if department_filter and organization:
        if department_filter == 'my_department' and request.user.department:
            orders_q = orders_q.filter(department=request.user.department)
        elif department_filter != 'all':
            try:
                department = Department.objects.get(id_2=department_filter, organization=organization)
                orders_q = orders_q.filter(department=department)
            except Department.DoesNotExist:
                pass

    # Creator filter
    creator_filter = request.GET.get('creator')
    if creator_filter and organization:
        if creator_filter == 'me':
            orders_q = orders_q.filter(created_by=request.user)
        elif creator_filter != 'all':
            try:
                creator = User.objects.get(id_2=creator_filter, organization=organization)
                orders_q = orders_q.filter(created_by=creator)
            except User.DoesNotExist:
                pass

    if filtr:
        status = filtr.split('|')[0]
        if status and status in [i[0] for i in ORDER_CHOICES]:
            orders_q = orders_q.filter(status=status)
            context['status'] = status

        from_date = filtr.split('|')[1]
        if from_date:
            from_date = datetime.datetime.strptime(
                from_date, '%Y-%m-%d %H:%M'
            ).replace(tzinfo=datetime.timezone.utc)
            orders_q = orders_q.filter(created_at__gte=from_date)
            context['from_date'] = from_date.strftime('%Y-%m-%d %H:%M')

        to_date = filtr.split('|')[2]
        if to_date:
            to_date = datetime.datetime.strptime(
                to_date, '%Y-%m-%d %H:%M'
            ).replace(tzinfo=datetime.timezone.utc)
            orders_q = orders_q.filter(created_at__lte=to_date)
            context['to_date'] = to_date.strftime('%Y-%m-%d %H:%M')

    if query:
        # Search
        orders_q = orders_q.filter(Q(id_2__icontains=query)).distinct()

    # Add department and creator options to context for filters
    if organization:
        context['departments'] = Department.objects.filter(organization=organization).order_by('name')
        context['creators'] = User.objects.filter(
            organization=organization, 
            client__isnull=False
        ).order_by('full_name')

    orders_q = orders_q.order_by('-created_at')
    paginator = Paginator(orders_q, 20)
    page_number = request.GET.get('page')
    context['orders'] = paginator.get_page(page_number)

    return render(request, 'orders/client_orders.html', context)


class PlaceOrderView(View, LoginRequiredMixin, UserPassesTestMixin):
    redirect_field_name = None

    def test_func(self):
        return is_client_check(self.request.user)

    def get(self, request):
        can_order = self.request.user.client.can_place_order()
        if not can_order[0]:
            # Cannot order
            messages.info(self.request, can_order[1])
            return redirect('/orders/')

        context = {
            'vehicle_types': VehicleType.objects.values_list('code', 'name').order_by('name'),
            'google_api_key': settings.GOOGLE_FRONTEND_API_KEY,
        }
        return render(self.request, 'orders/place_order.html', context)

    def post(self, request):
        can_order = self.request.user.client.can_place_order()
        if not can_order[0]:
            # Cannot order
            raise PermissionDenied

        intent = self.request.POST['intent']

        # Enhanced logging
        logger.info(f"Place order POST request with intent: {intent}")
        logger.info(f"Request data: {self.request.POST}")

        if intent == 'order_route':
            data = json.loads(self.request.POST['data'])
            logger.info(f"Order route data: {data}")

            # Check vehicle type
            vehicle_type = VehicleType.objects.filter(code=data['vehicle_type']).first()
            if not vehicle_type:
                return JsonResponse(data={'message': 'Vehicle type not available.'}, status=400)

            # Get route
            data['travel_mode'] = vehicle_type.travel_mode
            data['vehicle_type'] = vehicle_type.code
            data['client_id'] = self.request.user.client.pk

            # Ensure intermediates are properly formatted
            if 'locations' in data and 'intermediates' in data['locations']:
                logger.info(f"Found {len(data['locations']['intermediates'])} intermediates in request")

                # Validate each intermediate point has valid lat/lng
                for i, point in enumerate(data['locations']['intermediates']):
                    if not (isinstance(point, dict) and 'latitude' in point and 'longitude' in point):
                        logger.error(f"Invalid intermediate point format at index {i}: {point}")
                        return JsonResponse(data={'message': 'Invalid drop-off location format.'}, status=400)
            else:
                logger.info("No intermediates found in request")

            route = get_order_route(data)
            if route is None:
                logger.error("Failed to get order route")
                return HttpResponse(status=500)

            logger.info(f"Route calculated successfully: {route}")
            return JsonResponse(data=route, status=200)

        elif intent == 'place_order':
            # Clean order
            logger.info("Processing place_order intent")
            order_form = OrderForm(data=self.request.POST)
            if not order_form.is_valid():
                # Invalid order data
                logger.error(f"Order form errors: {order_form.errors}")
                msg = 'Unable to place order. Please check submitted data then try again.'
                return JsonResponse(data={'message': msg}, status=400)

            # Clean drop-offs
            drop_off_forms = []
            drop_offs_data = json.loads(request.POST['drop_offs'])
            logger.info(f"Processing {len(drop_offs_data)} drop-offs")

            for drop_off in drop_offs_data:
                dd = {}
                for key, val in drop_off.items():
                    key = key.replace('drop_off_', '')
                    dd[key] = val

                drop_off_form = OrderDropOffForm(data=dd)
                if not drop_off_form.is_valid():
                    # Invalid drop offs data
                    logger.error(f"Drop-off form errors: {drop_off_form.errors}")
                    msg = 'Unable to place order. Please check drop offs then try again.'
                    return JsonResponse(data={'message': msg}, status=400)

                # Drop off clean
                drop_off_forms.append(drop_off_form)

            # Get route
            route_data = {
                'locations': {
                    'pickup': {
                        'latitude': float(order_form.cleaned_data['pickup_latlng'].split(',')[0]),
                        'longitude': float(order_form.cleaned_data['pickup_latlng'].split(',')[1])
                    },
                    'destination': {
                        'latitude': float(order_form.cleaned_data['destination_latlng'].split(',')[0]),
                        'longitude': float(order_form.cleaned_data['destination_latlng'].split(',')[1])
                    },
                },
                'travel_mode': order_form.cleaned_data['vehicle_type'].travel_mode,
                'vehicle_type': order_form.cleaned_data['vehicle_type'].code,
                'client_id': self.request.user.client.pk
            }

            route_intermediates = []
            for d_form in drop_off_forms:
                route_intermediates.append({
                    'latitude': float(d_form.cleaned_data['latlng'].split(',')[0]),
                    'longitude': float(d_form.cleaned_data['latlng'].split(',')[1])
                })

            route_data['locations']['intermediates'] = route_intermediates
            logger.info(f"Generating final route with {len(route_intermediates)} intermediates")

            route = get_order_route(route_data)
            if route is None:
                msg = 'Unable to get order route. Please try again.'
                return JsonResponse(data={'message': msg}, status=500)

            # Reorder drop offs in optimized order
            if len(drop_off_forms) > 1 and 'optimizedIntermediateWaypointIndex' in route:
                logger.info(
                    f"Reordering drop-offs according to optimized route: {route['optimizedIntermediateWaypointIndex']}")
                temp = []
                for index in route['optimizedIntermediateWaypointIndex']:
                    temp.append(drop_off_forms[index])

                drop_off_forms = temp

            
            # Create order with department and creator tracking
            order = order_form.save(commit=False)
            order.client = self.request.user.client
            order.created_by = self.request.user
            order.department = self.request.user.department
            order.distance = route['distanceKm']
            order.rate_per_km = route.get('rate')
            order.price = route['price'] if route.get('model_type') != 'point-based' else 0
            order.est_duration = datetime.timedelta(seconds=int(route['duration'].replace('s', '')))
            order.route = {'polyline': route['polyline']['encodedPolyline']}
            order.save()

            # Create drop_offs
            for i, d_form in enumerate(drop_off_forms):
                drop_off = d_form.save(commit=False)
                drop_off.order = order
                drop_off.save()
                logger.info(f"Created drop-off {i + 1}: {drop_off.address}")

            # Create quote for point-based pricing
            if route.get('model_type') == 'point-based':
                from orders.models import OrderQuote
                from orders.utils_quotes import send_quote_created_email

                quote = OrderQuote(order=order, status='PENDING')
                if 'service_delivery_type' in request.POST:
                    quote.client_note = f"Service Delivery Type: {request.POST['service_delivery_type']}"
                quote.save()

                # Send email notification to admin
                send_quote_created_email(quote)

                logger.info(f"Created quote request for order: {order.id_2}")

            messages.success(self.request, 'Order placed successfully.')
            next_url = reverse('single_order', 'logistics.clients_urls', args=[order.id_2])
            return JsonResponse(data={'next_url': next_url}, status=200)


class ClientOrderView(View, LoginRequiredMixin, UserPassesTestMixin):
    redirect_field_name = None

    def test_func(self):
        return is_client_check(self.request.user)

    def get_order(self, id_2):
        user = self.request.user

        if user.organization:
            client_ids = Client.objects.filter(user__organization=user.organization).values_list('id', flat=True)
            return get_object_or_404(Order2, id_2=id_2, client_id__in=client_ids)
        else:
            return get_object_or_404(Order2, id_2=id_2, client=user.client)

    def get(self, request, id_2):
        order = self.get_order(id_2)
        drop_offs = OrderDropOff.objects.filter(order=order).order_by('id')
        context = {
            'order': order,
            'drop_offs': drop_offs,
            'route': order.route,
            'google_api_key': settings.GOOGLE_FRONTEND_API_KEY,
            'items_summary': order.get_items_summary(),
            'total_items': order.get_total_items(),
        }

        if order.driver:
            latest_location = PartnerLocation.objects.filter(
                partner=order.driver
            ).order_by('-timestamp').first()

            if latest_location:
                context['driver_location'] = {
                    'latitude': float(latest_location.latitude),
                    'longitude': float(latest_location.longitude),
                    'timestamp': latest_location.timestamp.strftime('%Y-%m-%d %H:%M:%S')
                }

        return render(self.request, 'orders/client_single_order.html', context)

    def post(self, request, id_2):
        order = self.get_order(id_2)
        intent = self.request.POST['intent']

        if intent == 'cancel':
            if order.status != 'PENDING':
                return JsonResponse(data={'message': 'Order not in pending state.'}, status=400)

            Order2.objects.filter(pk=order.pk).update(status='CANCELLED')

            try:
                from orders.models import OrderQuote
                quote = OrderQuote.objects.get(order=order)
                quote.status = 'DENIED'
                quote.save()

                from orders.utils_quotes import send_quote_decision_email
                send_quote_decision_email(quote, 'denied')
            except OrderQuote.DoesNotExist:
                pass

            messages.success(self.request, 'Order cancelled.')
            next_url = reverse('orders', 'logistics.clients_urls')
            return JsonResponse(data={'next_url': next_url}, status=200)

        elif intent == 'pay':
            if order.status != 'PENDING':
                return JsonResponse(data={'message': 'Order not in pending state.'}, status=400)

            form = PayOrderForm(data=self.request.POST)
            if not form.is_valid():
                return JsonResponse(data={'errors': form.errors}, status=400)

            event = {
                'event_type': 'ORDER_PAYMENT',
                'order_id': order.id_2,
                'user_id': self.request.user.id_2,
                'amount': int(order.price),
                'mpesa_number': form.cleaned_data['mpesa_number'],
                'business_id': settings.ELPHWAYS_BUSINESS_ID,
                'payment_type': 'C2B',
            }
            payment = init_elphways_payment(event)
            return elphways_c2b_payment_init_response(payment)

        elif intent == 'is_paid':
            if order.is_paid:
                messages.success(self.request, 'Order paid successfully. Please wait as it gets processed.')

            return JsonResponse(data={'is_paid': order.is_paid}, status=200)

        elif intent == 'pay_later':
            if order.status != 'PENDING':
                return JsonResponse(data={'message': 'Order not in pending state.'}, status=400)

            if not self.request.user.client.can_pay_later:
                return JsonResponse(data={'message': 'You are not eligible for the Pay Later program.'}, status=400)

            order.status = 'PROCESSING'
            order.save()
            messages.success(self.request, 'Your order will get processed shortly.')
            return JsonResponse(data={'callback': 'reload'}, status=200)


class ClientInvoicesView(View, LoginRequiredMixin, UserPassesTestMixin):
    redirect_field_name = None

    def test_func(self):
        return is_client_check(self.request.user)

    def get(self, request):
        client = request.user.client
        organization = request.user.organization

        if organization and (request.user.is_org_admin or client.is_primary):
            # Org admin or primary client: show all invoices for the organization
            client_ids = Client.objects.filter(user__organization=organization).values_list('id', flat=True)
            invoices_q = Invoice.objects.filter(client_id__in=client_ids)

            # Use the primary client for summary (if available)
            primary_client = Client.objects.filter(user__organization=organization, is_primary=True).first() or client

            context = {
                'summary': client_invoices_summary(primary_client),
                'organization': organization,
                'is_org_admin': True,
            }
        else:
            # Regular client, not an admin
            invoices_q = Invoice.objects.filter(client=client)
            context = {
                'summary': client_invoices_summary(client),
            }

        # Handle filtering
        filtr = request.GET.get('filter')
        if filtr:
            try:
                from_date_str, to_date_str = filtr.split('|')
                if from_date_str:
                    from_date = datetime.datetime.strptime(from_date_str.strip(), '%Y-%m-%d %H:%M').replace(
                        tzinfo=datetime.timezone.utc
                    )
                    invoices_q = invoices_q.filter(created_at__gte=from_date)
                    context['from_date'] = from_date.strftime('%Y-%m-%d %H:%M')

                if to_date_str:
                    to_date = datetime.datetime.strptime(to_date_str.strip(), '%Y-%m-%d %H:%M').replace(
                        tzinfo=datetime.timezone.utc
                    )
                    invoices_q = invoices_q.filter(created_at__lte=to_date)
                    context['to_date'] = to_date.strftime('%Y-%m-%d %H:%M')
            except Exception:
                context['filter_error'] = "Invalid date filter"

        invoices = invoices_q.order_by('-created_at')
        paginator = Paginator(invoices, 10)
        page_number = request.GET.get('page')
        context['invoices'] = paginator.get_page(page_number)

        return render(request, 'orders/client_invoices.html', context)

    def post(self, request):
        client = request.user.client
        organization = request.user.organization
        invoice_id = request.POST.get('invoice')
        intent = request.POST.get('intent')

        if not invoice_id or not intent:
            return JsonResponse({'error': 'Missing data'}, status=400)

        if organization and (request.user.is_org_admin or client.is_primary):
            client_ids = Client.objects.filter(user__organization=organization).values_list('id', flat=True)
            invoice = get_object_or_404(Invoice, id_2=invoice_id, client_id__in=client_ids)
        else:
            invoice = get_object_or_404(Invoice, id_2=invoice_id, client=client)

        if intent == 'pay_now':
            if invoice.is_paid:
                return JsonResponse({'message': f'Invoice {invoice.id_2} already paid.'}, status=400)

            form = MpesaNumberForm(request.POST)
            if not form.is_valid():
                return JsonResponse({'errors': form.errors}, status=400)

            event = {
                'event_type': 'INVOICE_PAYMENT',
                'invoice_id': invoice.id_2,
                'user_id': request.user.id_2,
                'amount': int(invoice.get_total_amount()),
                'mpesa_number': form.cleaned_data['mpesa_number'],
                'business_id': settings.ELPHWAYS_BUSINESS_ID,
                'payment_type': 'C2B',
            }
            payment = init_elphways_payment(event)
            return elphways_c2b_payment_init_response(payment)

        elif intent == 'invoice_paid':
            if invoice.is_paid:
                messages.success(request, f'Invoice {invoice.id_2} paid successfully.')
            return JsonResponse({'is_paid': invoice.is_paid}, status=200)

        return JsonResponse({'error': 'Invalid intent'}, status=400)


@login_required(redirect_field_name=None)
@user_passes_test(is_client_check, redirect_field_name=None)
def client_invoice_orders_view(request, id_2):
    try:
        # Try getting the invoice normally by client
        invoice = Invoice.objects.get(id_2=id_2, client=request.user.client)
    except Invoice.DoesNotExist:
        # If not found, allow org admin access if user is in same organization
        if request.user.is_org_admin:
            invoice = get_object_or_404(
                Invoice, id_2=id_2,
                client__user__organization=request.user.organization
            )
        else:
            return HttpResponseForbidden("Access denied.")

    # Now fetch related orders
    orders_q = Order2.objects.filter(invoice=invoice)

    query = request.GET.get('q')
    if query:
        orders_q = orders_q.filter(Q(id_2__icontains=query)).distinct()

    orders_q = orders_q.order_by('-created_at')
    paginator = Paginator(orders_q, 20)
    page_number = request.GET.get('page')

    context = {
        'invoice': invoice,
        'orders': paginator.get_page(page_number)
    }
    return render(request, 'orders/client_invoice_orders.html', context)


@login_required(redirect_field_name=None)
@user_passes_test(is_client_check, redirect_field_name=None)
def quote_response_view(request, id_2):
    order = get_object_or_404(Order2, id_2=id_2, client=request.user.client)
    quote = get_object_or_404(OrderQuote, order=order)

    if request.method == 'POST':
        action = request.POST.get('action')

        if quote.status != 'RESPONDED':
            messages.error(request, 'This quote is not awaiting your response.')
            return HttpResponseRedirect(f'/orders/{order.id_2}/')

        from orders.utils_quotes import send_quote_decision_email

        if action == 'accept':
            quote.status = 'ACCEPTED'
            quote.save()

            # Update order status to PROCESSING
            order.status = 'PROCESSING'
            order.save()

            # Send email notification to admin
            send_quote_decision_email(quote, 'accepted')

            messages.success(request, 'Quote accepted. Your order is now being processed.')
            return HttpResponseRedirect(f'/orders/{order.id_2}/')

        elif action == 'deny':
            quote.status = 'DENIED'
            quote.save()

            # Update order status to CANCELLED
            order.status = 'CANCELLED'
            order.save()

            # Send email notification to admin
            send_quote_decision_email(quote, 'denied')

            messages.info(request, 'Quote denied. Your order has been cancelled.')
            return HttpResponseRedirect(f'/orders/')
        else:
            messages.error(request, 'Invalid action.')
            return HttpResponseRedirect(f'/orders/{order.id_2}/quote/')

    context = {
        'quote': quote,
    }
    return render(request, 'orders/client_quote_response.html', context)


@login_required(redirect_field_name=None)
@user_passes_test(is_client_check, redirect_field_name=None)
@require_POST
def estimate_cost_view(request):
    form = CostEstimatorForm(data=request.POST)
    if not form.is_valid():
        # Invalid data
        for key, value in json.loads(form.errors.as_json()).items():
            for err in value:
                return JsonResponse(data={'message': err['message']}, status=400)

    rate_price = get_rate_price(
        form.cleaned_data['distance'],
        form.cleaned_data['vehicle_type'].code,
        request.user.client.pk
    )
    rate_price['price'] = f'{rate_price['price']:,}'
    if rate_price.get('rate'):
        rate_price['rate'] = f'{rate_price['rate']:,}'

    return JsonResponse(
        data={'rate_price': rate_price, 'callback': 'cost_estimator'},
        status=200
    )


@login_required(redirect_field_name=None)
@user_passes_test(is_client_check, redirect_field_name=None)
def order_items_view(request, id_2):
    """
    View to display order items details for a specific order
    """
    order = get_object_or_404(Order2, id_2=id_2, client=request.user.client)
    
    context = {
        'order': order,
        'items': order.items,
        'items_summary': order.get_items_summary(),
        'total_items': order.get_total_items(),
    }
    
    return render(request, 'orders/order_items.html', context)


@login_required(redirect_field_name=None)
@user_passes_test(is_client_check, redirect_field_name=None)
@require_POST
def update_order_items_view(request, id_2):
    """
    AJAX view to update order items (only for PENDING orders)
    """
    order = get_object_or_404(Order2, id_2=id_2, client=request.user.client)
    
    # Only allow updates for pending orders
    if order.status != 'PENDING':
        return JsonResponse({
            'error': 'Items can only be updated for pending orders.'
        }, status=400)
    
    try:
        items_data = json.loads(request.POST.get('items', '[]'))
        
        # Validate items data
        if not isinstance(items_data, list):
            return JsonResponse({'error': 'Items must be a list.'}, status=400)
        
        # Validate each item
        for i, item in enumerate(items_data):
            if not isinstance(item, dict):
                return JsonResponse({'error': f'Item {i+1} must be an object.'}, status=400)
            
            if 'type' not in item or 'quantity' not in item:
                return JsonResponse({'error': f'Item {i+1} missing required fields.'}, status=400)
            
            if not isinstance(item['type'], str) or not item['type'].strip():
                return JsonResponse({'error': f'Item {i+1} type must be a non-empty string.'}, status=400)
            
            if not isinstance(item['quantity'], int) or item['quantity'] < 1:
                return JsonResponse({'error': f'Item {i+1} quantity must be a positive integer.'}, status=400)
        
        # Update order items
        order.items = items_data
        order.save()
        
        return JsonResponse({
            'success': True,
            'message': 'Items updated successfully.',
            'items_summary': order.get_items_summary(),
            'total_items': order.get_total_items(),
        })
        
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON data.'}, status=400)
    except Exception as e:
        logger.error(f"Error updating order items: {str(e)}")
        return JsonResponse({'error': 'An error occurred while updating items.'}, status=500)


@login_required(redirect_field_name=None)
@user_passes_test(is_partner_check, redirect_field_name=None)
def partner_order_items_view(request, id_2):
    """
    View for partners to see order items details
    """
    order = get_object_or_404(Order2, id_2=id_2)
    partner = request.user.partner
    
    # Check authorization
    if not (order.vehicle_owner == partner or order.driver == partner):
        raise PermissionDenied
    
    context = {
        'order': order,
        'items': order.items,
        'items_summary': order.get_items_summary(),
        'total_items': order.get_total_items(),
    }
    
    return render(request, 'orders/partner_order_items.html', context)


class OrderItemsAPIView(View, LoginRequiredMixin):
    """
    API view to get order items as JSON
    """
    
    def get(self, request, id_2):
        # Try to get order based on user type
        if is_client_check(request.user):
            order = get_object_or_404(Order2, id_2=id_2, client=request.user.client)
        elif is_partner_check(request.user):
            order = get_object_or_404(Order2, id_2=id_2)
            partner = request.user.partner
            
            # Check authorization for partners
            if not (order.vehicle_owner == partner or order.driver == partner):
                return JsonResponse({'error': 'Access denied.'}, status=403)
        else:
            return JsonResponse({'error': 'Access denied.'}, status=403)
        
        return JsonResponse({
            'order_id': order.id_2,
            'items': order.items,
            'items_summary': order.get_items_summary(),
            'total_items': order.get_total_items(),
            'status': order.status,
        })


@login_required(redirect_field_name=None)
def orders_dashboard_view(request):
    """
    Dashboard view that shows different content based on user type
    """
    if is_client_check(request.user):
        return redirect('client_orders_view')
    elif is_partner_check(request.user):
        return redirect('partner_orders_view')
    else:
        return HttpResponseForbidden("Access denied.")


@login_required(redirect_field_name=None)
@require_POST
def bulk_update_items_view(request):
    """
    Bulk update items for multiple orders (admin functionality)
    """
    if not request.user.is_staff:
        return JsonResponse({'error': 'Access denied.'}, status=403)
    
    try:
        data = json.loads(request.body)
        order_ids = data.get('order_ids', [])
        items_template = data.get('items_template', [])
        
        if not order_ids or not items_template:
            return JsonResponse({'error': 'Missing required data.'}, status=400)
        
        # Validate items template
        for i, item in enumerate(items_template):
            if not isinstance(item, dict) or 'type' not in item or 'quantity' not in item:
                return JsonResponse({'error': f'Invalid item template at index {i}.'}, status=400)
        
        # Update orders
        updated_count = 0
        for order_id in order_ids:
            try:
                order = Order2.objects.get(id_2=order_id)
                if order.status == 'PENDING':  # Only update pending orders
                    order.items = items_template
                    order.save()
                    updated_count += 1
            except Order2.DoesNotExist:
                continue
        
        return JsonResponse({
            'success': True,
            'message': f'Updated items for {updated_count} orders.',
            'updated_count': updated_count,
        })
        
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON data.'}, status=400)
    except Exception as e:
        logger.error(f"Error in bulk update items: {str(e)}")
        return JsonResponse({'error': 'An error occurred.'}, status=500)