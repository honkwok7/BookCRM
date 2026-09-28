from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

User = get_user_model()


class AuthAPITests(APITestCase):
    def test_user_registration(self):
        payload = {
            "email": "test@example.com",
            "phone_number": "09120000000",
            "first_name": "Test",
            "last_name": "User",
            "password": "strongpass123",
            "accept_terms": True,
            "accept_privacy": True,
        }
        response = self.client.post("/api/register/", payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(User.objects.filter(email="test@example.com").exists())

    def test_user_login(self):
        User.objects.create_user(email="john@example.com", password="strongpass123")
        response = self.client.post(
            "/api/login/",
            {"email": "john@example.com", "password": "strongpass123"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("access", response.data)
        self.assertIn("refresh", response.data)
