import decimal
import re
import uuid

from django import forms
from django.utils import timezone

from utils.utils import parse_country_phone_no, validate_file_size, jpeg_uploaded_img, validate_mpesa_number
from vehicles.models import VehicleType
from .models import Order2, OrderDropOff
from django.core.exceptions import ValidationError
import json


class MultipleFileInput(forms.FileInput):
    """
    File Input widget that allows multiple file selection
    """
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    """
    Field that handles multiple file uploads using MultipleFileInput
    """
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", MultipleFileInput)
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        single_file_clean = super().clean
        if isinstance(data, (list, tuple)):
            result = [single_file_clean(d, initial) for d in data]
        else:
            result = single_file_clean(data, initial)
        return result


class DeliveryNoteForm(forms.ModelForm):
    delivery_note = forms.ImageField(validators=[validate_file_size], required=False)
    additional_notes = MultipleFileField(validators=[validate_file_size], required=False)

    class Meta:
        model = Order2
        fields = ['delivery_note']

    def clean(self):
        if OrderDropOff.objects.filter(order=self.instance, arrived=False).exists():
            self.add_error('delivery_note', 'Complete all drop offs to confirm delivery.')
        
        # Ensure at least one delivery note is provided
        if not self.cleaned_data.get('delivery_note') and not self.files.getlist('additional_notes'):
            self.add_error('delivery_note', 'At least one delivery note is required.')
        
        return self.cleaned_data

    def save(self, *args, **kwargs):
        obj = super(DeliveryNoteForm, self).save(commit=False)
        obj.delivered_at = timezone.now()
        obj.status = 'DELIVERED'
        
        # Process the main delivery note
        delivery_notes = []
        if self.cleaned_data.get('delivery_note'):
            jpeg_uploaded_img(obj.delivery_note)
            delivery_notes.append(obj.delivery_note.name)
        
        # Process additional notes
        for file in self.files.getlist('additional_notes'):
            field_file = self.fields['additional_notes'].clean(file)
            jpeg_uploaded_img(field_file)
            delivery_notes.append(field_file.name)
        
        # Save to the JSONField
        obj.delivery_notes = delivery_notes
        return obj


class OrderForm(forms.ModelForm):
    pickup_date = forms.DateTimeField(required=False, input_formats=['%Y-%m-%d %H:%M'])
    vehicle_type = forms.ModelChoiceField(queryset=VehicleType.objects.all(), to_field_name='code')
    items = forms.CharField(required=False, widget=forms.HiddenInput())

    class Meta:
        model = Order2
        fields = [
            'pickup_address', 'pickup_latlng', 'pickup_contact', 'pickup_note', 'pickup_date',
            'destination_address', 'destination_latlng', 'destination_contact', 'destination_note',
            'vehicle_type', 'items'
        ]

    def clean_items(self):
        items_data = self.cleaned_data.get('items')
        
        # If no items data provided, return empty list
        if not items_data:
            return []
        
        try:
            # Parse JSON string to Python object
            items = json.loads(items_data)
            
            # Validate that it's a list
            if not isinstance(items, list):
                raise ValidationError("Items must be a list.")
            
            # Validate each item in the list
            for i, item in enumerate(items):
                if not isinstance(item, dict):
                    raise ValidationError(f"Item {i+1} must be an object.")
                
                # Check required fields
                if 'type' not in item:
                    raise ValidationError(f"Item {i+1} is missing 'type' field.")
                
                if 'quantity' not in item:
                    raise ValidationError(f"Item {i+1} is missing 'quantity' field.")
                
                # Validate type
                if not isinstance(item['type'], str) or not item['type'].strip():
                    raise ValidationError(f"Item {i+1} type must be a non-empty string.")
                
                # Validate quantity
                if not isinstance(item['quantity'], int) or item['quantity'] < 1:
                    raise ValidationError(f"Item {i+1} quantity must be a positive integer.")
                
                # Optional: limit type length
                if len(item['type']) > 200:
                    raise ValidationError(f"Item {i+1} type cannot exceed 200 characters.")
            
            return items
            
        except json.JSONDecodeError:
            raise ValidationError("Items data is not valid JSON.")
    
    def save(self, commit=True):
        instance = super().save(commit=False)
        
        # Set the items field from cleaned data
        instance.items = self.cleaned_data.get('items', [])
        
        if commit:
            instance.save()
        
        return instance


class OrderDropOffForm(forms.ModelForm):
    class Meta:
        model = OrderDropOff
        fields = [
            'address', 'latlng', 'contact', 'note'
        ]


class PayOrderForm(forms.Form):
    mpesa_number = forms.CharField(max_length=50, validators=[validate_mpesa_number])

    def clean_mpesa_number(self):
        """
        Validates and cleans Mpesa phone number to ensure:
        1. Removes '+' prefix if present
        2. Starts with 254
        3. Has correct length (12 digits)
        4. Contains only numbers
        """
        number = self.cleaned_data['mpesa_number']

        # Remove any whitespace
        number = number.strip()

        # Remove '+' prefix if present
        if number.startswith('+'):
            number = number[1:]

        # Check if number starts with 254
        if not number.startswith('254'):
            raise forms.ValidationError('Phone number must start with 254')

        # Remove all non-digit characters
        number = re.sub(r'\D', '', number)

        # Check length (should be 12 digits)
        if len(number) != 12:
            raise forms.ValidationError('Phone number must be 12 digits long')

        # Check if the number contains only digits
        if not number.isdigit():
            raise forms.ValidationError('Phone number must contain only digits')

        # Validate number format (254XXXXXXXXX)
        pattern = r'^254[7,1]\d{8}$'
        if not re.match(pattern, number):
            raise forms.ValidationError('Invalid Mpesa phone number format')

        return number


class CostEstimatorForm(forms.Form):
    distance = forms.DecimalField(min_value=decimal.Decimal('0.01'))
    vehicle_type = forms.ModelChoiceField(queryset=VehicleType.objects.all(), to_field_name='code')


class ConfirmDropOffForm(forms.ModelForm):
    delivery_note = forms.ImageField(validators=[validate_file_size], required=False)
    additional_notes = MultipleFileField(validators=[validate_file_size], required=False)
    action = forms.ChoiceField(choices=[
        ('complete', 'Complete Dropoff'),
        ('skip', 'Skip Dropoff'),
        ('reschedule', 'Reschedule Dropoff')
    ], required=True)
    skip_reason = forms.CharField(max_length=500, required=False)
    rescheduled_date = forms.DateTimeField(required=False, input_formats=['%Y-%m-%d %H:%M'])

    class Meta:
        model = OrderDropOff
        fields = ['delivery_note', 'skip_reason', 'rescheduled_date']
    
    def clean(self):
        cleaned_data = super().clean()
        action = cleaned_data.get('action')
        
        if action == 'complete':
            # Ensure at least one delivery note is provided for completed dropoffs
            if not cleaned_data.get('delivery_note') and not self.files.getlist('additional_notes'):
                self.add_error('delivery_note', 'At least one delivery note is required when completing a dropoff.')
        elif action == 'skip':
            # Ensure a skip reason is provided
            if not cleaned_data.get('skip_reason'):
                self.add_error('skip_reason', 'A reason is required when skipping a dropoff.')
        elif action == 'reschedule':
            # Ensure a skip reason is provided for rescheduling too
            if not cleaned_data.get('skip_reason'):
                self.add_error('skip_reason', 'A reason is required when rescheduling a dropoff.')
        
        return cleaned_data

    def save(self, *args, **kwargs):
        obj = super(ConfirmDropOffForm, self).save(commit=False)
        action = self.cleaned_data.get('action')
        
        if action == 'complete':
            obj.arrived = True
            obj.dropped_off_at = timezone.now()
            obj.status = 'COMPLETED'
            
            # Process the main delivery note
            delivery_notes = []
            if self.cleaned_data.get('delivery_note'):
                jpeg_uploaded_img(obj.delivery_note)
                delivery_notes.append(obj.delivery_note.name)
            
            # Process additional notes
            for file in self.files.getlist('additional_notes'):
                field_file = self.fields['additional_notes'].clean(file)
                # Create a new file name with the correct path
                file_name = f"drop_offs/delivery_notes/{timezone.now().strftime('%Y/%m/%d')}/{uuid.uuid4()}.jpeg"
                # Save the file with the correct path
                obj.delivery_note.storage.save(file_name, field_file)
                delivery_notes.append(file_name)
            
            # Save to the JSONField
            obj.delivery_notes = delivery_notes
        
        elif action == 'skip':
            obj.status = 'SKIPPED'
            obj.skip_reason = self.cleaned_data.get('skip_reason')
        
        elif action == 'reschedule':
            obj.status = 'RESCHEDULED'
            obj.skip_reason = self.cleaned_data.get('skip_reason')
        
        obj.save()
        return obj
