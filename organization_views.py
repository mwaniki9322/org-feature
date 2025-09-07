import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.mail import send_mail
from django.core.paginator import Paginator
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404, redirect
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.crypto import get_random_string
from django.views import View

from accounts.forms import AddOrganizationUserForm, UpdateOrganizationUserForm
from accounts.models import User, Client, Organization
from logistics.settings import DEFAULT_FROM_EMAIL, SITE_NAME
from utils.utils import parse_country_phone_no

logger = logging.getLogger(__name__)


def is_org_admin_check(user):
    """Check if user is an organization admin"""
    return user.is_org_admin and user.organization is not None


def get_primary_client_or_admin(organization):
    """
    Get the primary client or organization admin to reassign orders to.
    Priority: Primary client -> Organization admin -> First available user
    """
    # First try to find primary client
    primary_client = User.objects.filter(
        organization=organization,
        client__is_primary=True
    ).first()
    
    if primary_client:
        return primary_client
    
    # If no primary client, find an organization admin
    org_admin = User.objects.filter(
        organization=organization,
        is_org_admin=True
    ).first()
    
    if org_admin:
        return org_admin
    
    # If no admin, get the first available user in the organization
    fallback_user = User.objects.filter(organization=organization).first()
    
    return fallback_user


def reassign_user_orders(removed_user, reassign_to_user):
    """
    Reassign all orders from removed user to the specified user.
    This function should be customized based on your Order model structure.
    """
    try:
        # Import here to avoid circular imports - adjust the import path as needed
        from orders.models import Order  # Adjust this import path to match your project structure
        
        # Find all orders associated with the removed user
        # Adjust the field names based on your Order model
        orders_to_reassign = Order.objects.filter(
            client=removed_user  # or whatever field links orders to users
        )
        
        reassigned_count = 0
        for order in orders_to_reassign:
            order.client = reassign_to_user  # Adjust field name as needed
            order.save()
            reassigned_count += 1
            
        return reassigned_count
        
    except ImportError:
        logger.error("Could not import Order model. Please check the import path.")
        return 0
    except Exception as e:
        logger.error(f"Error reassigning orders: {e}")
        return 0


class OrganizationUserListView(View, LoginRequiredMixin, UserPassesTestMixin):
    """View for listing organization users"""
    
    redirect_field_name = ""
    
    def test_func(self):
        return is_org_admin_check(self.request.user)
    
    def get(self, request):
        organization = request.user.organization
        users = User.objects.filter(organization=organization).order_by('-is_org_admin', 'email')
        
        paginator = Paginator(users, 20)  # Show 20 per page
        page_number = request.GET.get('page')
        
        context = {
            'organization': organization,
            'users': paginator.get_page(page_number),
        }
        return render(request, 'accounts/organization/user_list.html', context)


class OrganizationUserAddView(View, LoginRequiredMixin, UserPassesTestMixin):
    """View for adding users to an organization"""
    
    redirect_field_name = ""
    
    def test_func(self):
        return is_org_admin_check(self.request.user)
    
    def get(self, request):
        context = {
            'organization': request.user.organization,
        }
        return render(request, 'accounts/organization/user_add.html', context)
    
    def post(self, request):
        organization = request.user.organization
        form = AddOrganizationUserForm(request.POST)
        
        if not form.is_valid():
            errors = {}
            for field, error_list in form.errors.items():
                errors[field] = [str(error) for error in error_list]
            return JsonResponse({'errors': errors}, status=400)
        
        email = form.cleaned_data['email']
        full_name = form.cleaned_data['full_name']
        phone_number = form.cleaned_data.get('phone_number')
        is_org_admin = form.cleaned_data.get('is_org_admin', False)
        
        # Check if admin user is verified
        admin_is_verified = False
        if hasattr(request.user, 'client') and request.user.client.is_verified:
            admin_is_verified = True
        
        # Check if user already exists
        existing_user = User.objects.filter(email=email).first()
        if existing_user:
            if existing_user.organization:
                return JsonResponse({
                    'errors': {'email': ['User with this email already belongs to an organization.']}
                }, status=400)
            
            # Add existing user to organization
            existing_user.organization = organization
            existing_user.is_org_admin = is_org_admin
            
            # Update phone number
            existing_user.phone_number = phone_number
                
            existing_user.save()
            
            # If user is a client, update is_primary to False and set verification status
            if hasattr(existing_user, 'client'):
                existing_user.client.is_primary = False
                # If admin is verified, automatically verify the user
                if admin_is_verified:
                    existing_user.client.is_verified = True
                existing_user.client.save()
                
            messages.success(request, f'User {email} added to organization.')
            return JsonResponse({
                'next_url': reverse('organization_users', urlconf='logistics.clients_urls')
            }, status=200)
        
        # Create new user
        password = get_random_string(12)
        new_user = User.objects.create_user(
            email=email,
            password=password,
            full_name=full_name,
            phone_number=phone_number,
            organization=organization,
            is_org_admin=is_org_admin
        )
        
        # Create client for the user
        client = Client.objects.create(
            user=new_user,
            _type='company',
            is_primary=False,
            # If admin is verified, automatically verify the user
            is_verified=admin_is_verified
        )
        
        # Send email with login details
        try:
            context = {
                'user': new_user,
                'password': password,
                'site_name': SITE_NAME,
                'admin': request.user,
                'organization': organization
            }
            
            email_html = render_to_string('accounts/emails/new_organization_user.html', context)
            email_subject = f'You have been added to {organization.name} on {SITE_NAME}'
            
            send_mail(
                subject=email_subject,
                message=f'You have been added to {organization.name} by {request.user.full_name}. Your login email is {email} and password is {password}.',
                from_email=DEFAULT_FROM_EMAIL,
                recipient_list=[email],
                html_message=email_html,
                fail_silently=False
            )
        except Exception as e:
            logger.error(f"Failed to send email to new organization user: {e}")
        
        messages.success(request, f'User {email} created and added to organization. Login details: Email: {email}, Password: {password}')
        return JsonResponse({
            'next_url': reverse('organization_users', urlconf='logistics.clients_urls')
        }, status=200)


class OrganizationUserEditView(View, LoginRequiredMixin, UserPassesTestMixin):
    """View for editing organization users"""
    
    redirect_field_name = ""
    
    def test_func(self):
        return is_org_admin_check(self.request.user)
    
    def get(self, request, user_id):
        organization = request.user.organization
        user = get_object_or_404(User, id_2=user_id, organization=organization)
        
        context = {
            'organization': organization,
            'edit_user': user,
        }
        return render(request, 'accounts/organization/user_edit.html', context)
    
    def post(self, request, user_id):
        organization = request.user.organization
        user = get_object_or_404(User, id_2=user_id, organization=organization)
        
        # Don't allow changing admin status of self
        if user == request.user and 'is_org_admin' in request.POST:
            return JsonResponse({
                'errors': {'__all__': ['You cannot change your own admin status.']}
            }, status=400)
        
        form = UpdateOrganizationUserForm(request.POST, instance=user)
        
        if not form.is_valid():
            errors = {}
            for field, error_list in form.errors.items():
                errors[field] = [str(error) for error in error_list]
            return JsonResponse({'errors': errors}, status=400)
        
        # Update user details
        if 'full_name' in form.cleaned_data and form.cleaned_data['full_name']:
            user.full_name = form.cleaned_data['full_name']
        
        if 'phone_number' in form.cleaned_data:
            user.phone_number = form.cleaned_data['phone_number']
        
        if 'is_org_admin' in form.cleaned_data and user != request.user:
            user.is_org_admin = form.cleaned_data['is_org_admin']
        
        user.save()
        
        messages.success(request, f'User {user.email} updated.')
        return JsonResponse({
            'next_url': reverse('organization_users', urlconf='logistics.clients_urls')
        }, status=200)


class OrganizationUserRemoveView(View, LoginRequiredMixin, UserPassesTestMixin):
    """View for removing users from an organization"""
    
    redirect_field_name = ""
    
    def test_func(self):
        return is_org_admin_check(self.request.user)
    
    def post(self, request, user_id):
        organization = request.user.organization
        user = get_object_or_404(User, id_2=user_id, organization=organization)
        
        # Don't allow removing self
        if user == request.user:
            return JsonResponse({
                'message': 'You cannot remove yourself from the organization.'
            }, status=400)
        
        # Use database transaction to ensure data consistency
        with transaction.atomic():
            # Find user to reassign orders to
            reassign_to_user = get_primary_client_or_admin(organization)
            
            # Make sure we don't reassign to the user being removed
            if reassign_to_user == user:
                # Get another user from the organization (excluding the one being removed)
                reassign_to_user = User.objects.filter(
                    organization=organization
                ).exclude(id=user.id).first()
            
            reassigned_orders_count = 0
            
            # Reassign orders if there's someone to reassign to
            if reassign_to_user:
                reassigned_orders_count = reassign_user_orders(user, reassign_to_user)
                
                if reassigned_orders_count > 0:
                    logger.info(
                        f"Reassigned {reassigned_orders_count} orders from {user.email} "
                        f"to {reassign_to_user.email} in organization {organization.name}"
                    )
            else:
                logger.warning(
                    f"No available user found to reassign orders for removed user {user.email} "
                    f"in organization {organization.name}"
                )
            
            # Remove user from organization
            user.organization = None
            user.is_org_admin = False
            user.save()
        
        # Prepare success message
        success_message = f'User {user.email} removed from organization.'
        if reassigned_orders_count > 0:
            success_message += f' {reassigned_orders_count} orders reassigned to {reassign_to_user.email}.'
        
        messages.success(request, success_message)
        return JsonResponse({
            'next_url': reverse('organization_users', urlconf='logistics.clients_urls')
        }, status=200)


class OrganizationUserResetPasswordView(View, LoginRequiredMixin, UserPassesTestMixin):
    """View for resetting user passwords"""
    
    redirect_field_name = ""
    
    def test_func(self):
        return is_org_admin_check(self.request.user)
    
    def post(self, request, user_id):
        organization = request.user.organization
        user = get_object_or_404(User, id_2=user_id, organization=organization)
        
        # Generate new password
        new_password = get_random_string(12)
        user.set_password(new_password)
        user.save()
        
        # Send email with new password
        try:
            context = {
                'user': user,
                'new_password': new_password,
                'site_name': SITE_NAME,
                'admin': request.user,
                'organization': organization
            }
            
            email_html = render_to_string('accounts/password_reset/password_reset_by_admin.html', context)
            email_subject = f'Your password has been reset on {SITE_NAME}'
            
            send_mail(
                subject=email_subject,
                message=f'Your password has been reset by {request.user.full_name}. Your new password is {new_password}.',
                from_email=DEFAULT_FROM_EMAIL,
                recipient_list=[user.email],
                html_message=email_html,
                fail_silently=False
            )
        except Exception as e:
            logger.error(f"Failed to send password reset email: {e}")
        
        messages.success(request, f'Password reset for {user.email}. New password: {new_password}')
        return JsonResponse({
            'next_url': reverse('organization_user_edit', args=[user.id_2],urlconf='logistics.clients_urls')
        }, status=200)