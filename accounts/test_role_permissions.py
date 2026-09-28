from datetime import time, timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import UserRole
from appointments.models import Appointment, AppointmentStatus
from specialists.models import Specialist, WorkingHour

User = get_user_model()


class RolePolicyAPITests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.customer = User.objects.create_user(
            "customer", password="StrongRolePass123", role=UserRole.CUSTOMER
        )
        cls.other_customer = User.objects.create_user("other-customer", role=UserRole.CUSTOMER)
        cls.staff_customer = User.objects.create_user(
            "staff-customer", role=UserRole.CUSTOMER, is_staff=True
        )
        cls.super_customer = User.objects.create_superuser("super-customer", role=UserRole.CUSTOMER)
        cls.owner = User.objects.create_user("owner", role=UserRole.OWNER)
        cls.admin = User.objects.create_user("admin", role=UserRole.ADMIN)
        cls.specialist_user = User.objects.create_user("specialist", role=UserRole.SPECIALIST)
        cls.other_specialist_user = User.objects.create_user(
            "other-specialist", role=UserRole.SPECIALIST
        )
        cls.unlinked_specialist = User.objects.create_user(
            "unlinked-specialist", role=UserRole.SPECIALIST
        )
        cls.linked_customer = User.objects.create_user("linked-customer", role=UserRole.CUSTOMER)
        cls.specialist = Specialist.objects.create(
            name="A", profession="GP", user=cls.specialist_user
        )
        cls.other_specialist = Specialist.objects.create(
            name="B", profession="GP", user=cls.other_specialist_user
        )
        cls.customer_profile = Specialist.objects.create(
            name="C", profession="GP", user=cls.linked_customer
        )
        cls.day = timezone.localdate() + timedelta(days=7)
        for specialist in (cls.specialist, cls.other_specialist, cls.customer_profile):
            WorkingHour.objects.create(
                specialist=specialist,
                day=cls.day.weekday(),
                start_time=time(9),
                end_time=time(17),
            )

    def authenticate(self, user):
        self.client.force_authenticate(user)

    def appointment(self, user=None, specialist=None, state=AppointmentStatus.PENDING):
        return Appointment.objects.create(
            user=user or self.other_customer,
            specialist=specialist or self.specialist,
            date=self.day,
            time=time(10),
            duration=30,
            status=state,
        )

    def book(self, **extra):
        return self.client.post(
            "/api/appointments/",
            {
                "specialist": self.specialist.pk,
                "date": str(self.day),
                "time": "12:00",
                **extra,
            },
            format="json",
        )

    def mutate(self, appointment, action):
        return self.client.patch(
            f"/api/appointments/{appointment.pk}/{action}/",
            {"date": str(self.day), "time": "11:00"} if action == "reschedule" else {},
            format="json",
        )

    def assert_appointment_actions(self, actor, specialist=None):
        self.authenticate(actor)
        transitions = {
            "confirm": (AppointmentStatus.PENDING, AppointmentStatus.CONFIRMED),
            "cancel": (AppointmentStatus.PENDING, AppointmentStatus.CANCELLED),
            "complete": (AppointmentStatus.CONFIRMED, AppointmentStatus.COMPLETED),
            "no-show": (AppointmentStatus.CONFIRMED, AppointmentStatus.NO_SHOW),
            "reschedule": (AppointmentStatus.CONFIRMED, AppointmentStatus.CONFIRMED),
        }
        for action, (before, after) in transitions.items():
            with self.subTest(role=actor.role, action=action):
                appointment = self.appointment(specialist=specialist, state=before)
                response = self.mutate(appointment, action)
                self.assertEqual(response.status_code, 200, response.data)
                appointment.refresh_from_db()
                self.assertEqual(appointment.status, after)
                if action == "reschedule":
                    self.assertEqual(appointment.time, time(11))
                appointment.delete()

    def assert_actions_denied(self, actor, *, specialist=None, customer=None, actions=None):
        self.authenticate(actor)
        appointment = self.appointment(
            user=customer,
            specialist=specialist,
            state=AppointmentStatus.CONFIRMED,
        )
        original = Appointment.objects.values().get(pk=appointment.pk)
        for action in actions or ("confirm", "cancel", "complete", "no-show", "reschedule"):
            with self.subTest(role=actor.role, action=action):
                response = self.mutate(appointment, action)
                self.assertEqual(response.status_code, 403, response.data)
                self.assertEqual(Appointment.objects.values().get(pk=appointment.pk), original)
        appointment.delete()

    def assert_management_allowed(self, actor):
        self.authenticate(actor)
        response = self.client.post("/api/specialists/", {"name": "New", "profession": "GP"})
        self.assertEqual(response.status_code, 201, response.data)
        pk = response.data["id"]
        for method in ("put", "patch"):
            response = getattr(self.client, method)(
                f"/api/specialists/{pk}/",
                {"name": "Updated", "profession": "GP"},
                format="json",
            )
            self.assertEqual(response.status_code, 200, response.data)
        response = self.client.post(
            f"/api/specialists/{pk}/working-hours/",
            {
                "day": self.day.weekday(),
                "start_time": "09:00",
                "end_time": "17:00",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        wh_pk = response.data["id"]
        response = self.client.delete(f"/api/specialists/{pk}/working-hours/{wh_pk}/")
        self.assertEqual(response.status_code, 204)
        self.assertFalse(WorkingHour.objects.filter(pk=wh_pk).exists())
        response = self.client.delete(f"/api/specialists/{pk}/")
        self.assertEqual(response.status_code, 204)
        self.assertFalse(Specialist.objects.filter(pk=pk).exists())

    def assert_management_denied(self, actor):
        self.authenticate(actor)
        response = self.client.get("/api/appointments/")
        self.assertEqual(response.status_code, 403, response.data)
        response = self.client.post(
            "/api/specialists/",
            {
                "name": "Escalation",
                "profession": "GP",
                "user": actor.pk,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 403, response.data)
        self.assertFalse(Specialist.objects.filter(name="Escalation").exists())
        pk = self.other_specialist.pk
        for method in ("put", "patch", "delete"):
            response = getattr(self.client, method)(
                f"/api/specialists/{pk}/",
                {"name": "Changed", "profession": "GP"},
                format="json",
            )
            self.assertEqual(response.status_code, 403, response.data)
        self.other_specialist.refresh_from_db()
        self.assertEqual(self.other_specialist.name, "B")
        response = self.client.post(
            f"/api/specialists/{pk}/working-hours/",
            {
                "day": self.day.weekday(),
                "start_time": "17:00",
                "end_time": "18:00",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 403, response.data)
        wh = WorkingHour.objects.get(specialist_id=pk)
        response = self.client.delete(f"/api/specialists/{pk}/working-hours/{wh.pk}/")
        self.assertEqual(response.status_code, 403, response.data)
        self.assertTrue(WorkingHour.objects.filter(pk=wh.pk).exists())

    def test_owner_can_perform_all_administrative_appointment_actions(self):
        self.assert_appointment_actions(self.owner)

    def test_nonstaff_admin_can_perform_all_administrative_appointment_actions(self):
        self.assertFalse(self.admin.is_staff)
        self.assert_appointment_actions(self.admin)

    def test_owner_can_manage_specialists_and_working_hours(self):
        self.assert_management_allowed(self.owner)

    def test_nonstaff_admin_can_manage_specialists_and_working_hours(self):
        self.assert_management_allowed(self.admin)

    def test_admin_and_owner_can_list_all_and_any_specialist_appointments(self):
        first = self.appointment()
        second = self.appointment(user=self.customer, specialist=self.other_specialist)
        for actor in (self.owner, self.admin):
            with self.subTest(role=actor.role):
                self.authenticate(actor)
                response = self.client.get("/api/appointments/")
                self.assertEqual(response.status_code, 200, response.data)
                self.assertEqual(
                    {row["id"] for row in response.data["results"]}, {first.pk, second.pk}
                )
                for specialist, appointment in (
                    (self.specialist, first),
                    (self.other_specialist, second),
                ):
                    response = self.client.get(f"/api/specialists/{specialist.pk}/appointments/")
                    self.assertEqual(response.status_code, 200, response.data)
                    self.assertEqual(
                        [row["id"] for row in response.data["results"]], [appointment.pk]
                    )

    def test_owner_admin_and_specialists_cannot_book_with_or_without_profiles(self):
        for actor in (self.owner, self.admin):
            Specialist.objects.create(name=actor.username, profession="GP", user=actor)
        for actor in (self.owner, self.admin, self.specialist_user, self.unlinked_specialist):
            with self.subTest(role=actor.role, user=actor.username):
                self.authenticate(actor)
                response = self.book()
                self.assertEqual(response.status_code, 403, response.data)
        self.assertFalse(Appointment.objects.exists())

    def test_unlinked_owner_and_admin_cannot_book(self):
        for actor in (self.owner, self.admin):
            self.authenticate(actor)
            response = self.book()
            self.assertEqual(response.status_code, 403, response.data)
        self.assertFalse(Appointment.objects.exists())

    def test_administrative_roles_do_not_change_django_staff_flags(self):
        for actor in (self.owner, self.admin):
            self.authenticate(actor)
            self.assertEqual(self.client.get("/api/appointments/").status_code, 200)
            actor.refresh_from_db()
            self.assertFalse(actor.is_staff)
            self.assertFalse(actor.is_superuser)

    def test_staff_customer_has_no_administrative_api_access(self):
        self.assert_management_denied(self.staff_customer)
        self.assert_actions_denied(self.staff_customer)

    def test_superuser_customer_has_no_administrative_api_access(self):
        self.assert_management_denied(self.super_customer)
        self.assert_actions_denied(self.super_customer)
        self.super_customer.refresh_from_db()
        self.assertTrue(self.super_customer.is_staff)
        self.assertTrue(self.super_customer.is_superuser)

    def test_staff_customer_can_still_book_as_customer(self):
        self.authenticate(self.staff_customer)
        response = self.book()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Appointment.objects.get().user_id, self.staff_customer.pk)

    def test_specialist_can_perform_assigned_actions(self):
        self.assert_appointment_actions(self.specialist_user)

    def test_specialist_cannot_modify_another_specialists_appointment(self):
        self.assert_actions_denied(self.specialist_user, specialist=self.other_specialist)

    def test_specialist_customer_ownership_does_not_bypass_assignment(self):
        self.assert_actions_denied(
            self.specialist_user,
            specialist=self.other_specialist,
            customer=self.specialist_user,
        )

    def test_specialist_lists_only_assigned_specialist_appointments(self):
        own = self.appointment()
        self.appointment(specialist=self.other_specialist)
        self.authenticate(self.specialist_user)
        response = self.client.get(f"/api/specialists/{self.specialist.pk}/appointments/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual([row["id"] for row in response.data["results"]], [own.pk])
        response = self.client.get(f"/api/specialists/{self.other_specialist.pk}/appointments/")
        self.assertEqual(response.status_code, 403, response.data)

    def test_specialist_cannot_manage_specialists_or_others_working_hours(self):
        self.assert_management_denied(self.specialist_user)

    def test_specialist_cannot_manage_own_working_hours(self):
        self.authenticate(self.specialist_user)
        pk = self.specialist.pk
        response = self.client.post(
            f"/api/specialists/{pk}/working-hours/",
            {
                "day": self.day.weekday(),
                "start_time": "17:00",
                "end_time": "18:00",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 403, response.data)
        wh = WorkingHour.objects.get(specialist_id=pk)
        response = self.client.delete(f"/api/specialists/{pk}/working-hours/{wh.pk}/")
        self.assertEqual(response.status_code, 403, response.data)
        self.assertTrue(WorkingHour.objects.filter(pk=wh.pk).exists())

    def test_unlinked_specialist_cannot_use_assigned_operations(self):
        self.assert_actions_denied(self.unlinked_specialist, customer=self.unlinked_specialist)
        self.authenticate(self.unlinked_specialist)
        response = self.client.get(f"/api/specialists/{self.specialist.pk}/appointments/")
        self.assertEqual(response.status_code, 403, response.data)
        self.assert_management_denied(self.unlinked_specialist)

    def test_customer_can_book_only_for_themselves(self):
        self.authenticate(self.customer)
        response = self.book(user=self.other_customer.pk)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Appointment.objects.get().user_id, self.customer.pk)

    def test_customer_can_cancel_and_reschedule_own_appointment(self):
        appointment = self.appointment(user=self.customer)
        self.authenticate(self.customer)
        response = self.mutate(appointment, "reschedule")
        self.assertEqual(response.status_code, 200, response.data)
        response = self.mutate(appointment, "cancel")
        self.assertEqual(response.status_code, 200, response.data)
        appointment.refresh_from_db()
        self.assertEqual(appointment.time, time(11))
        self.assertEqual(appointment.status, AppointmentStatus.CANCELLED)

    def test_customer_cannot_modify_other_customers_appointment(self):
        self.assert_actions_denied(self.customer)

    def test_customer_cannot_perform_specialist_actions_on_own_appointment(self):
        self.assert_actions_denied(
            self.customer,
            customer=self.customer,
            actions=("confirm", "complete", "no-show"),
        )

    def test_customer_cannot_manage_specialists_or_working_hours(self):
        self.assert_management_denied(self.customer)

    def test_linked_customer_does_not_gain_specialist_authority(self):
        self.assert_actions_denied(self.linked_customer, specialist=self.customer_profile)
        self.authenticate(self.linked_customer)
        response = self.client.get(f"/api/specialists/{self.customer_profile.pk}/appointments/")
        self.assertEqual(response.status_code, 403, response.data)
        self.assert_management_denied(self.linked_customer)

    def test_linked_customer_can_still_book_and_manage_own_booking(self):
        self.authenticate(self.linked_customer)
        response = self.book()
        self.assertEqual(response.status_code, 201, response.data)
        appointment = Appointment.objects.get(pk=response.data["id"])
        self.assertEqual(appointment.user_id, self.linked_customer.pk)
        self.assertEqual(self.mutate(appointment, "reschedule").status_code, 200)
        self.assertEqual(self.mutate(appointment, "cancel").status_code, 200)

    def test_linked_customer_with_staff_flag_is_still_only_customer(self):
        self.linked_customer.is_staff = True
        self.linked_customer.save(update_fields=["is_staff"])
        self.assert_management_denied(self.linked_customer)
        self.assert_actions_denied(self.linked_customer, specialist=self.customer_profile)
        self.authenticate(self.linked_customer)
        response = self.book()
        self.assertEqual(response.status_code, 201, response.data)

    def test_linked_specialist_with_staff_flag_does_not_gain_admin_authority(self):
        self.specialist_user.is_staff = True
        self.specialist_user.save(update_fields=["is_staff"])
        self.assert_management_denied(self.specialist_user)
        self.assert_actions_denied(self.specialist_user, specialist=self.other_specialist)

    def test_all_roles_my_appointments_remains_ownership_filtered(self):
        for actor in (
            self.owner,
            self.admin,
            self.specialist_user,
            self.unlinked_specialist,
            self.customer,
            self.linked_customer,
            self.staff_customer,
        ):
            with self.subTest(actor=actor.username):
                own = self.appointment(user=actor)
                other = self.appointment(specialist=self.other_specialist)
                self.authenticate(actor)
                response = self.client.get("/api/my-appointments/")
                self.assertEqual(response.status_code, 200, response.data)
                self.assertEqual([row["id"] for row in response.data["results"]], [own.pk])
                own.delete()
                other.delete()

    def test_public_specialist_working_hour_and_availability_reads_remain_public(self):
        for actor in (
            None,
            self.customer,
            self.admin,
            self.owner,
            self.specialist_user,
            self.unlinked_specialist,
        ):
            self.authenticate(actor)
            for url in (
                "/api/specialists/",
                f"/api/specialists/{self.specialist.pk}/",
                f"/api/specialists/{self.specialist.pk}/working-hours/",
                f"/api/specialists/{self.specialist.pk}/available-slots/?date={self.day}",
            ):
                with self.subTest(actor=actor, url=url):
                    response = self.client.get(url)
                    self.assertEqual(response.status_code, 200, response.data)

    def test_unauthenticated_protected_operations_still_require_authentication(self):
        self.authenticate(None)
        appointment = self.appointment()
        self.assertEqual(self.book().status_code, 401)
        for url in (
            "/api/appointments/",
            "/api/my-appointments/",
            f"/api/specialists/{self.specialist.pk}/appointments/",
        ):
            self.assertEqual(self.client.get(url).status_code, 401)
        for action in ("confirm", "cancel", "complete", "no-show", "reschedule"):
            self.assertEqual(self.mutate(appointment, action).status_code, 401)
        self.assertEqual(self.client.post("/api/specialists/", {}).status_code, 401)

    def test_owner_admin_working_hour_delete_remains_parent_scoped(self):
        wh = WorkingHour.objects.get(specialist=self.other_specialist)
        for actor in (self.owner, self.admin):
            self.authenticate(actor)
            response = self.client.delete(
                f"/api/specialists/{self.specialist.pk}/working-hours/{wh.pk}/"
            )
            self.assertEqual(response.status_code, 404, response.data)
            self.assertTrue(WorkingHour.objects.filter(pk=wh.pk).exists())

    def test_role_change_is_enforced_with_existing_jwt(self):
        self.authenticate(None)
        response = self.client.post(
            "/api/login/",
            {
                "email": self.customer.email,
                "password": "StrongRolePass123",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access']}")
        self.assertEqual(self.client.get("/api/appointments/").status_code, 403)
        User.objects.filter(pk=self.customer.pk).update(role=UserRole.ADMIN)
        self.assertEqual(self.client.get("/api/appointments/").status_code, 200)
        self.assertEqual(self.book().status_code, 403)
        User.objects.filter(pk=self.customer.pk).update(role=UserRole.CUSTOMER)
        self.assertEqual(self.client.get("/api/appointments/").status_code, 403)

    def assert_registration_cannot_elevate(self, role):
        self.authenticate(None)
        response = self.client.post(
            "/api/register/",
            {
                "email": "new-user@example.test",
                "password": "StrongNewPass123",
                "role": role,
                "is_staff": True,
                "is_superuser": True,
                "accept_terms": True,
                "accept_privacy": True,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        user = User.objects.get(email="new-user@example.test")
        self.assertEqual(user.role, UserRole.CUSTOMER)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.authenticate(user)
        self.assertEqual(self.client.get("/api/appointments/").status_code, 403)

    def test_registration_cannot_create_owner(self):
        self.assert_registration_cannot_elevate(UserRole.OWNER)

    def test_registration_cannot_create_admin(self):
        self.assert_registration_cannot_elevate(UserRole.ADMIN)

    def test_registration_cannot_create_specialist(self):
        self.assert_registration_cannot_elevate(UserRole.SPECIALIST)

    def test_profile_updates_cannot_change_role_or_staff_flags(self):
        self.authenticate(self.customer)
        for method in ("patch", "put"):
            for role in (UserRole.OWNER, UserRole.ADMIN, UserRole.SPECIALIST):
                with self.subTest(method=method, role=role):
                    response = getattr(self.client, method)(
                        "/api/profile/",
                        {
                            "role": role,
                            "is_staff": True,
                            "is_superuser": True,
                            "first_name": "Allowed edit",
                        },
                        format="json",
                    )
                    self.assertEqual(response.status_code, 200, response.data)
                    self.customer.refresh_from_db()
                    self.assertEqual(self.customer.role, UserRole.CUSTOMER)
                    self.assertFalse(self.customer.is_staff)
                    self.assertFalse(self.customer.is_superuser)
                    self.assertEqual(self.customer.first_name, "Allowed edit")
                    self.assertEqual(self.client.get("/api/appointments/").status_code, 403)

    def test_admin_specialist_creation_cannot_assign_customer_role_or_link(self):
        self.authenticate(self.admin)
        response = self.client.post(
            "/api/specialists/",
            {
                "name": "Unlinked",
                "profession": "GP",
                "user": self.customer.pk,
                "role": UserRole.SPECIALIST,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        profile = Specialist.objects.get(pk=response.data["id"])
        self.assertIsNone(profile.user_id)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.role, UserRole.CUSTOMER)

    def test_unknown_role_does_not_fall_back_to_staff_or_customer_privileges(self):
        self.staff_customer.role = "unknown"
        self.staff_customer.save(update_fields=["role"])
        self.assert_management_denied(self.staff_customer)
        self.assert_actions_denied(self.staff_customer, customer=self.staff_customer)
        self.authenticate(self.staff_customer)
        self.assertEqual(self.book().status_code, 403)
