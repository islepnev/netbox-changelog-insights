from django.urls import path

from . import views

urlpatterns = [
    path('', views.PulseView.as_view(), name='pulse'),
    path('contributors/', views.ContributorsView.as_view(), name='contributors'),
    path('activity/', views.ActivityView.as_view(), name='activity'),
    path('chains/', views.ChainsView.as_view(), name='chains'),
    path('change-sets/<uuid:request_id>/', views.ChangeSetView.as_view(), name='changeset'),
]
