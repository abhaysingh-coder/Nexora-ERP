from django import forms
from django.contrib.auth.forms import AuthenticationForm


class UserLoginForm(AuthenticationForm):
    username = forms.CharField(
        label='User ID',
        widget=forms.TextInput(
            attrs={
                'autofocus': True,
                'autocomplete': 'username',
                'class': 'form-control',
                'placeholder': 'Enter your user ID',
            }
        ),
    )
    password = forms.CharField(
        label='Password',
        strip=False,
        widget=forms.PasswordInput(
            attrs={
                'autocomplete': 'current-password',
                'class': 'form-control',
                'placeholder': 'Enter your password',
            }
        ),
    )
