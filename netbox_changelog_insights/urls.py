from django.urls import path
from django.views.generic import RedirectView

from . import views

urlpatterns = [
    # Pulse has its own path: NetBox marks a menu item active when its URL is a prefix of the
    # current one, so the plugin root would mark Pulse on every page. The root redirects to it.
    path('', RedirectView.as_view(pattern_name='plugins:netbox_changelog_insights:pulse', query_string=True),
         name='index'),
    path('pulse/', views.PulseView.as_view(), name='pulse'),
    path('contributors/', views.ContributorsView.as_view(), name='contributors'),
    path('user/', views.UserView.as_view(), name='user'),
    path('activity/', views.ActivityView.as_view(), name='activity'),
    path('hotspots/', views.HotspotsView.as_view(), name='hotspots'),
    path('hotspots/field/', views.FieldChangesView.as_view(), name='field_changes'),
    path('scope/', views.ScopeView.as_view(), name='scope'),
    path('chains/', views.ChainsView.as_view(), name='chains'),
    path('change-sets/<uuid:request_id>/', views.ChangeSetView.as_view(), name='changeset'),
]
