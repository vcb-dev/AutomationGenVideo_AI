import jwt
from django.conf import settings
from django.contrib.auth.models import User
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed


class NestJWTAuthentication(BaseAuthentication):
    """Validate JWT tokens issued by the NestJS auth service."""

    keyword = 'Bearer'

    def authenticate(self, request):
        auth_header = request.META.get('HTTP_AUTHORIZATION', '')
        if not auth_header.startswith(f'{self.keyword} '):
            return None

        token = auth_header[len(self.keyword) + 1:]
        if not token:
            return None

        base_secret = getattr(settings, 'JWT_SECRET', None)
        if not base_secret:
            return None

        secrets_to_try = [base_secret]
        boot_suffix = getattr(settings, 'JWT_BOOT_SUFFIX', '')
        if boot_suffix:
            secrets_to_try.insert(0, f'{base_secret}.{boot_suffix}')

        payload = None
        last_error = None
        for secret in secrets_to_try:
            try:
                payload = jwt.decode(token, secret, algorithms=['HS256'])
                break
            except jwt.InvalidSignatureError:
                # Wrong secret — try next
                continue
            except jwt.InvalidTokenError as e:
                # Expired, malformed, etc. — record and stop (signature matched)
                last_error = e
                break

        if last_error is not None:
            raise AuthenticationFailed(str(last_error))

        if payload is None:
            return None

        email = payload.get('email', '')
        user_id = payload.get('sub', '')
        if not email and not user_id:
            raise AuthenticationFailed('Invalid token payload')

        user, _ = User.objects.get_or_create(
            username=email or f'nest_{user_id}',
            defaults={'email': email},
        )
        return (user, payload)
