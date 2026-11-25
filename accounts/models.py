from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """Custom user. Email is used as the login (stored in `username`)."""

    phone = models.CharField(max_length=20, blank=True)

    @property
    def display_name(self):
        return self.get_full_name() or self.email or self.username
