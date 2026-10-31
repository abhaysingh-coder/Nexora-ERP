from django.urls import path

from . import views

app_name = 'organizations'

urlpatterns = [
    path('select/', views.select_organization, name='select'),
    path('access-required/', views.access_required, name='access_required'),
]
