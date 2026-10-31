from django.contrib.auth.views import LoginView, LogoutView
from django.urls import path

from .forms import UserLoginForm
from .views import HomeView

urlpatterns = [
    path('', HomeView.as_view(), name='home'),
    path(
        'login/',
        LoginView.as_view(template_name='registration/login.html', authentication_form=UserLoginForm),
        name='login',
    ),
    path('logout/', LogoutView.as_view(next_page='login'), name='logout'),
]
