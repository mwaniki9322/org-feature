import datetime
import string

from django.conf import settings
from django.core.files.storage import storages
from django.db import models
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.utils import timezone
from typing_extensions import Any
from django.utils.module_loading import import_string

from utils.utils import gen_unique_id_2

protected_fs = storages["private"]

ORDER_CHOICES = (
    ('PENDING', 'Pending'),
    ('PROCESSING', 'Processing'),
    ('CANCELLED', 'Cancelled'),
    ('CONFIRMED', 'Confirmed'),
    ('IN_TRANSIT', 'In transit'),
    ('DELIVERED', 'Delivered'),
    ('COMPLETED', 'Completed')
)


class Order2(models.Model):
    client = models.ForeignKey('accounts.Client', on_delete=models.SET_NULL, null=True)

    # Pickup
    pickup_address = models.CharField(max_length=500)
    pickup_latlng = models.CharField(max_length=150)
    pickup_contact = models.CharField(max_length=50, blank=True, null=True)
    pickup_note = models.TextField(max_length=500, blank=True, null=True)
    pickup_date = models.DateTimeField(blank=True, null=True)

    # Destination
    destination_address = models.CharField(max_length=500)
    destination_latlng = models.CharField(max_length=150)
    destination_contact = models.CharField(max_length=50, blank=True, null=True)
    destination_note = models.TextField(max_length=500, blank=True, null=True)

    # Items
    items = models.JSONField(
        default=list,
        blank=True,
        help_text="List of items with type and quantity: [{'type': 'Electronics', 'quantity': 2}]"
    )

    # Vehicle
    vehicle_type = models.ForeignKey('vehicles.VehicleType', on_delete=models.SET_NULL, null=True)
    vehicle = models.ForeignKey('vehicles.Vehicle', on_delete=models.SET_NULL, null=True)
    vehicle_owner = models.ForeignKey(
        'accounts.Partner',
        on_delete=models.SET_NULL,
        null=True,
        related_name='order2_vehicle_owner'
    )
    driver = models.ForeignKey(
        'accounts.Partner',
        on_delete=models.SET_NULL,
        null=True,
        related_name='order2_driver'
    )

    # Distance, rates and estimated time
    distance = models.DecimalField(max_digits=7, decimal_places=2)  # Distance in km
    rate_per_km = models.DecimalField(max_digits=13, decimal_places=2, null=True)  # Rate per km
    price = models.DecimalField(max_digits=13, decimal_places=2, null=True)
    partner_rate = models.DecimalField(max_digits=13, decimal_places=2, null=True)  # Rate per km
    partner_pay = models.DecimalField(max_digits=13, decimal_places=2, null=True)
    est_duration = models.DurationField(null=True)
    route = models.JSONField(null=True)

    # Others
    created_at = models.DateTimeField(auto_now_add=True)
    confirmed_at = models.DateTimeField(null=True)
    delivery_started_at = models.DateTimeField(null=True)
    delivered_at = models.DateTimeField(null=True)
    expected_delivery = models.DurationField(null=True)
    delivery_note = models.FileField(
        upload_to='delivery_notes/%Y/%m/%d/',
        storage=protected_fs,
        null=True
    )
    delivery_notes = models.JSONField(
        null=True,
        blank=True,
        default=list,
        help_text="List of delivery note file paths"
    )
    status = models.CharField(choices=ORDER_CHOICES, max_length=50, default='PENDING')
    is_paid = models.BooleanField(default=False)
    id_2 = models.CharField(max_length=50, editable=False, unique=True)
    invoice = models.ForeignKey('Invoice', on_delete=models.SET_NULL, null=True)

    class Meta:
        verbose_name = 'Order'
        verbose_name_plural = 'Orders'

    def save(self, *args, **kwargs):
        # Save id_2 on create
        if not self.id_2:
            self.id_2 = gen_unique_id_2(model=self.__class__, length=8, chars=string.digits)

        super(Order2, self).save()

    def __str__(self):
        return f'Order: {self.id_2}'

    def get_pickup_latlng(self):
        return {
            'lat': float(self.pickup_latlng.split(',')[0]),
            'lng': float(self.pickup_latlng.split(',')[1])
        }

    def get_destination_latlng(self):
        return {
            'lat': float(self.destination_latlng.split(',')[0]),
            'lng': float(self.destination_latlng.split(',')[1])
        }

    def get_est_duration_hm(self):
        seconds = int(self.est_duration.total_seconds())
        return f"{seconds // 3600}h:{(seconds % 3600) // 60}min"

    def is_picked(self):
        return self.status in ['IN_TRANSIT', 'DELIVERED', 'COMPLETED']

    def is_delivered(self):
        return self.status in ['DELIVERED', 'COMPLETED']

    def show_map(self):
        return self.status in ['PENDING', 'PROCESSING', 'CONFIRMED', 'IN_TRANSIT']

    def delivery_time(self):
        return self.delivered_at - self.delivery_started_at

    def get_commission(self):
        # If price is null, return 0
        if self.price is None:
            return 0

        # If partner_pay is null, return the full price
        if self.partner_pay is None:
            return self.price

        # Otherwise, calculate commission normally
        return self.price - self.partner_pay

    def all_dropoffs_handled(self):
        """
        Check if all dropoffs for this order have been handled properly.
        Returns True only if:
        1. All dropoffs are COMPLETED (no SCHEDULED, SKIPPED, or RESCHEDULED dropoffs)
        2. OR all dropoffs are either COMPLETED or RESCHEDULED (no SCHEDULED or SKIPPED dropoffs)
        """
        dropoffs = self.dropoffs.all()
        if not dropoffs.exists():
            return True  # No dropoffs for this order
        
        # Check if any dropoffs are still in SCHEDULED status
        has_scheduled = self.dropoffs.filter(status='SCHEDULED').exists()
        if has_scheduled:
            return False
        
        # Check if any dropoffs are in SKIPPED status - these must be completed before delivery
        has_skipped = self.dropoffs.filter(status='SKIPPED').exists()
        if has_skipped:
            return False
        
        # At this point, all dropoffs are either COMPLETED or RESCHEDULED, which is acceptable
        return True

    def has_skipped_dropoffs(self):
        """
        Check if this order has any skipped dropoffs.
        Returns True if there are any skipped dropoffs, False otherwise.
        """
        return self.dropoffs.filter(status='SKIPPED').exists()
        
    def get_next_dropoff(self):
        # Implementation of get_next_dropoff method
        pass

    def get_delivery_note_urls(self):
        """Returns a list of full URLs for delivery notes."""
        if not self.delivery_notes:
            return []
        
        urls = []
        storage_class = import_string(settings.STORAGES["private"]["BACKEND"])
        storage = storage_class()
        
        for note_path in self.delivery_notes:
            urls.append(storage.url(note_path))
        
        return urls

    def get_total_items(self):
        """Return total quantity of all items"""
        return sum(item.get('quantity', 0) for item in self.items)
    
    def get_items_summary(self):
        """Return a readable summary of items"""
        if not self.items:
            return "No items specified"
        return ", ".join([f"{item.get('quantity', 1)} x {item.get('type', 'Unknown')}" for item in self.items])


class OrderDropOff(models.Model):
    DROPOFF_STATUS_CHOICES = (
        ('SCHEDULED', 'Scheduled'),
        ('COMPLETED', 'Completed'),
        ('SKIPPED', 'Skipped'),
        ('RESCHEDULED', 'Rescheduled')
    )
    
    order = models.ForeignKey(Order2, on_delete=models.CASCADE, related_name='dropoffs')
    address = models.CharField(max_length=500)
    latlng = models.CharField(max_length=150)
    contact = models.CharField(max_length=50, blank=True, null=True)
    note = models.TextField(max_length=500, blank=True, null=True)
    arrived = models.BooleanField(default=False)
    dropped_off_at = models.DateTimeField(null=True)
    delivery_note = models.FileField(
        upload_to='drop_offs/delivery_notes/%Y/%m/%d/',
        storage=protected_fs,
        null=True
    )
    delivery_notes = models.JSONField(
        null=True,
        blank=True,
        default=list,
        help_text="List of delivery note file paths"
    )
    status = models.CharField(
        max_length=20, 
        choices=DROPOFF_STATUS_CHOICES, 
        default='SCHEDULED'
    )
    skip_reason = models.TextField(max_length=500, blank=True, null=True)
    rescheduled_date = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f'{self.address} - {self.order}'

    def get_latlng(self):
        lat_str = self.latlng.split(',')[0].replace('Â°', '')
        lng_str = self.latlng.split(',')[1].replace('Â°', '')
        return {
            'lat': float(lat_str),
            'lng': float(lng_str)
        }
        
    def is_completed(self):
        return self.status == 'COMPLETED'
        
    def is_skipped(self):
        return self.status == 'SKIPPED'
        
    def is_rescheduled(self):
        return self.status == 'RESCHEDULED'
        
    def resume(self):
        """Resume a skipped dropoff by changing its status back to SCHEDULED"""
        if self.status == 'SKIPPED':
            self.status = 'SCHEDULED'
            self.save()
            return True
        return False

    def get_delivery_note_urls(self):
        """Returns a list of full URLs for delivery notes."""
        if not self.delivery_notes:
            return []
        
        urls = []
        storage_class = import_string(settings.STORAGES["private"]["BACKEND"])
        storage = storage_class()
        
        for note_path in self.delivery_notes:
            urls.append(storage.url(note_path))
        
        return urls


class Invoice(models.Model):
    client = models.ForeignKey('accounts.Client', on_delete=models.SET_NULL, null=True)
    is_paid = models.BooleanField(default=False)
    created_at = models.DateTimeField(editable=False)
    due_date = models.DateTimeField(editable=False)
    id_2 = models.CharField(max_length=50, editable=False, unique=True)

    def __str__(self):
        return f'Invoice: {self.id_2}'

    def save(self, *args, **kwargs):
        # Save created at on create
        if not self.created_at:
            self.created_at = timezone.now()

        # Save due date on create
        if not self.due_date:
            self.due_date = self.created_at + datetime.timedelta(days=settings.INVOICE_DAYS)

        # Save id_2 on create
        if not self.id_2:
            self.id_2 = gen_unique_id_2(model=self.__class__, length=8, chars=string.digits)

        super(Invoice, self).save()

    def get_total_amount(self):
        return Order2.objects.filter(invoice=self).aggregate(models.Sum('price'))['price__sum']

    def total_orders(self):
        return Order2.objects.filter(invoice=self).count()

    def payment_method(self):
        return 'mpesa' if self.get_total_amount() <= settings.MPESA_MAX_TRANS else 'manual'


class OrderQuote(models.Model):
    QUOTE_STATUS_CHOICES = (
        ('PENDING', 'Pending'),
        ('RESPONDED', 'Responded'),
        ('ACCEPTED', 'Accepted'),
        ('DENIED', 'Denied'),
    )

    order = models.OneToOneField(Order2, on_delete=models.CASCADE, related_name='quote')
    status = models.CharField(max_length=50, choices=QUOTE_STATUS_CHOICES, default='PENDING')
    price = models.DecimalField(max_digits=13, decimal_places=2, null=True, blank=True)
    admin_note = models.TextField(max_length=500, blank=True, null=True)
    client_note = models.TextField(max_length=500, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    responded_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f'Quote for Order: {self.order.id_2}'

    def save(self, *args, **kwargs):
        if self.status == 'RESPONDED' and not self.responded_at:
            self.responded_at = timezone.now()
        super(OrderQuote, self).save(*args, **kwargs)


# Store the previous status to detect changes
_order_previous_status = {}
_dropoff_previous_status = {}


@receiver(pre_save, sender=Order2)
def capture_previous_order_status(sender, instance, **kwargs):
    """Capture the previous status before saving to detect status changes."""
    if instance.pk:  # Only for existing instances
        try:
            old_instance = Order2.objects.get(pk=instance.pk)
            _order_previous_status[instance.pk] = old_instance.status
        except Order2.DoesNotExist:
            pass


@receiver(pre_save, sender=OrderDropOff)
def capture_previous_dropoff_status(sender, instance, **kwargs):
    """Capture the previous dropoff status before saving to detect status changes."""
    if instance.pk:  # Only for existing instances
        try:
            old_instance = OrderDropOff.objects.get(pk=instance.pk)
            _dropoff_previous_status[instance.pk] = old_instance.status
        except OrderDropOff.DoesNotExist:
            pass


@receiver(post_save, sender=Order2)
def handle_order_status_change(sender, instance, created, **kwargs):
    """
    Signal handler to automatically try to assign a vehicle when an order is updated to "PROCESSING" status
    and send email notifications to clients for all status changes.
    
    Note: This signal is only triggered when using save() method, not when using QuerySet.update().
    Make sure to use order.save() instead of Order2.objects.filter(...).update() when changing order status.
    """
    import logging
    logger = logging.getLogger(__name__)
    logger.info(f"Signal handler triggered for order {instance.id_2} with status {instance.status}")

    # Get the previous status
    previous_status = _order_previous_status.get(instance.pk)
    
    # Skip email for new orders created with PENDING status to avoid duplicate emails
    if created and instance.status == 'PENDING':
        logger.info(f"Skipping email for new order {instance.id_2} created with PENDING status")
    else:
        # Send client notification email for status changes
        if not created or instance.status != 'PENDING':
            # Import here to avoid circular import
            from orders.utils import notify_client_status_change
            
            logger.info(f"Sending client notification email for order {instance.id_2} - Status: {instance.status}")
            notify_client_status_change(instance)

    # Existing vehicle assignment logic
    from orders.utils import try_auto_assign_vehicle

    # Check if the order is in processing state
    if instance.status == 'PROCESSING' and not instance.vehicle:
        logger.info(f"Attempting to auto-assign vehicle for order {instance.id_2}")
        # Try to auto-assign a vehicle
        result = try_auto_assign_vehicle(instance.id)
        logger.info(f"Auto-assignment result for order {instance.id_2}: {'Success' if result else 'Failed'}")
    else:
        if instance.status != 'PROCESSING':
            logger.info(f"Order {instance.id_2} status is not PROCESSING, no auto-assignment attempted")
        elif instance.vehicle:
            logger.info(
                f"Order {instance.id_2} already has vehicle {instance.vehicle.reg_no}, no auto-assignment attempted")

    # Clean up the stored previous status
    if instance.pk in _order_previous_status:
        del _order_previous_status[instance.pk]


@receiver(post_save, sender=OrderDropOff)
def handle_dropoff_status_change(sender, instance, created, **kwargs):
    """
    Signal handler to send email notifications to clients for dropoff status changes.
    
    Note: This signal is only triggered when using save() method, not when using QuerySet.update().
    Make sure to use dropoff.save() instead of OrderDropOff.objects.filter(...).update() when changing dropoff status.
    """
    import logging
    logger = logging.getLogger(__name__)
    logger.info(f"Dropoff signal handler triggered for order {instance.order.id_2} - Dropoff: {instance.address} - Status: {instance.status}")

    # Get the previous status
    previous_status = _dropoff_previous_status.get(instance.pk)
    
    # Skip email for new dropoffs created with SCHEDULED status to avoid duplicate emails
    if created and instance.status == 'SCHEDULED':
        logger.info(f"Skipping email for new dropoff created with SCHEDULED status for order {instance.order.id_2}")
    else:
        # Send client notification email for dropoff status changes
        if not created or instance.status != 'SCHEDULED':
            # Import here to avoid circular import
            from orders.utils import notify_client_dropoff_change
            
            logger.info(f"Sending client dropoff notification email for order {instance.order.id_2} - Dropoff Status: {instance.status}")
            notify_client_dropoff_change(instance)

    # Clean up the stored previous status
    if instance.pk in _dropoff_previous_status:
        del _dropoff_previous_status[instance.pk]