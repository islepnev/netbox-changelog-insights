from django.urls import path

from . import views

urlpatterns = [
    path('activity/', views.ActivityView.as_view(), name='activity'),
]
