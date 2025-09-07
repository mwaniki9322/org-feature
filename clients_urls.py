from django.urls import path, include
from django.views.generic import RedirectView

from accounts import views as accounts_views
from accounts.urls import urlpatterns as accounts_urls_patterns
from orders.urls import clients_patterns as clients_orders_patterns
from utils import views as utils_views
from orders import views as orders_views


app_name = 'clients'
urlpatterns = [
    path("", RedirectView.as_view(url="/dashboard/"), name='index'),
    path("dashboard/", accounts_views.client_dashboard_view, name="dashboard"),
    path("orders/", include(clients_orders_patterns)),

    path("invoices/", orders_views.ClientInvoicesView.as_view(), name="invoices"),
    path("invoices/<str:id_2>/orders/", orders_views.client_invoice_orders_view, name="invoice_orders"),

    path("pdf-render/<str:token>/", utils_views.pdf_render_view, name='pdf_render'),
    path("pdf-download/", utils_views.pdf_download_view, name='pdf_download'),
    path("orders-items-pdf/", utils_views.orders_items_pdf_download, name='orders_items_pdf_download'),
    path("excel-download/", utils_views.excel_download_view, name='excel_download'),
    path("orders-items-excel/", utils_views.orders_items_excel_download, name='orders_items_excel_download'),
] + accounts_urls_patterns
