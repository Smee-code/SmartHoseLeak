"""
Signing in with either a username or an email address.

Households were built around short usernames (hh002) and most have no email
on file, while staff and anyone who has added an email would rather type that.
One backend accepts both so nobody has to remember which their account uses.

Email is not unique on the user table, so a login by email only succeeds when
exactly one active account carries that address: an ambiguous email is treated
as no match rather than a guess about which person meant to sign in.
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend


class EmailOrUsernameBackend(ModelBackend):
    def authenticate(self, request, username=None, password=None, **kwargs):
        User = get_user_model()
        if not username or password is None:
            return None

        login = username.strip()
        user = User.objects.filter(username=login).first()
        if user is None and "@" in login:
            matches = list(User.objects.filter(email__iexact=login)[:2])
            if len(matches) == 1:
                user = matches[0]

        if user is None:
            # Run the hasher once anyway so a missing account takes the same
            # time as a wrong password, which otherwise leaks who exists.
            User().set_password(password)
            return None

        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None
