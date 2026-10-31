from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import redirect
from django.views import View


class HomeView(LoginRequiredMixin, View):
    def get(self, request, *args, **kwargs):
        return redirect('dashboard')
