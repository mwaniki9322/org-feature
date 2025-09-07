from django.urls import path
from . import views


partners_patterns = (
    [
        path("", views.partner_orders_view, name="orders"),
        path("<str:id_2>/", views.PartnerOrderView.as_view(), name="single_order"),
    ]
)

clients_patterns = (
    [
        path("", views.client_orders_view, name="orders"),
        path("place/", views.PlaceOrderView.as_view(), name="place_order"),
        path("estimate-cost/", views.estimate_cost_view, name="estimate_order_cost"),
        path("<str:id_2>/", views.ClientOrderView.as_view(), name="single_order"),
        path("<str:id_2>/quote/", views.quote_response_view, name="quote_response"),
    ]
)
