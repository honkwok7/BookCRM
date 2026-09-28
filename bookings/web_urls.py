from django.urls import path

from bookings.web_views import BookingSuccessView, PublicBookingPageView

urlpatterns = [
    path("book/<slug:slug>/", PublicBookingPageView.as_view(), name="public-booking"),
    path("book/success/<str:reference>/", BookingSuccessView.as_view(), name="booking-success"),
]
