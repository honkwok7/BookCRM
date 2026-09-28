# M2.1 step 1 of 3: additive CRM fields (email becomes optional).

import django.db.models.deletion
import django.db.models.functions.text
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("bookings", "0001_initial"),
        ("organizations", "0002_membership_capabilities_and_suspension"),
        ("staff", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="customer",
            name="address_line1",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="customer",
            name="address_line2",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="customer",
            name="alerts",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="customer",
            name="anonymized_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="customer",
            name="assigned_staff",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="assigned_customers",
                to="staff.staffprofile",
            ),
        ),
        migrations.AddField(
            model_name="customer",
            name="birthday",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="customer",
            name="city",
            field=models.CharField(blank=True, max_length=120),
        ),
        migrations.AddField(
            model_name="customer",
            name="consent_updated_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="customer",
            name="country",
            field=models.CharField(blank=True, max_length=2),
        ),
        migrations.AddField(
            model_name="customer",
            name="created_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="created_customers",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="customer",
            name="email_consent",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="customer",
            name="first_name",
            field=models.CharField(blank=True, max_length=150),
        ),
        migrations.AddField(
            model_name="customer",
            name="gender",
            field=models.CharField(blank=True, max_length=50),
        ),
        migrations.AddField(
            model_name="customer",
            name="last_name",
            field=models.CharField(blank=True, max_length=150),
        ),
        migrations.AddField(
            model_name="customer",
            name="marketing_consent",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="customer",
            name="postal_code",
            field=models.CharField(blank=True, max_length=20),
        ),
        migrations.AddField(
            model_name="customer",
            name="preferred_contact_method",
            field=models.CharField(
                blank=True,
                choices=[
                    ("email", "Email"),
                    ("sms", "SMS"),
                    ("phone", "Phone call"),
                    ("none", "Do not contact"),
                ],
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name="customer",
            name="preferred_language",
            field=models.CharField(blank=True, max_length=10),
        ),
        migrations.AddField(
            model_name="customer",
            name="preferred_name",
            field=models.CharField(blank=True, max_length=150),
        ),
        migrations.AddField(
            model_name="customer",
            name="preferred_staff",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="preferring_customers",
                to="staff.staffprofile",
            ),
        ),
        migrations.AddField(
            model_name="customer",
            name="pronouns",
            field=models.CharField(blank=True, max_length=50),
        ),
        migrations.AddField(
            model_name="customer",
            name="region",
            field=models.CharField(blank=True, max_length=120),
        ),
        migrations.AddField(
            model_name="customer",
            name="secondary_phone",
            field=models.CharField(blank=True, max_length=30),
        ),
        migrations.AddField(
            model_name="customer",
            name="sms_consent",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="customer",
            name="source",
            field=models.CharField(
                blank=True,
                choices=[
                    ("public_booking", "Online booking"),
                    ("reception", "Reception"),
                    ("staff", "Staff"),
                    ("referral", "Referral"),
                    ("walk_in", "Walk-in"),
                    ("import", "Import"),
                    ("other", "Other"),
                ],
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="customer",
            name="status",
            field=models.CharField(
                choices=[
                    ("active", "Active"),
                    ("inactive", "Inactive"),
                    ("archived", "Archived"),
                    ("anonymized", "Anonymized"),
                ],
                default="active",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="customer",
            name="email",
            field=models.EmailField(blank=True, max_length=254),
        ),
    ]
