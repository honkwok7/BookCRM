from django.urls import path

from core.web_views import FeaturesPageView, LandingPageView, PricingPageView

urlpatterns = [
    path("", LandingPageView.as_view(), name="landing"),
    path("features/", FeaturesPageView.as_view(), name="features"),
    path("pricing/", PricingPageView.as_view(), name="pricing"),
]
