# Token Encryption Setup Guide

## Overview

Page access tokens from Facebook are now encrypted before being stored in the database using **Fernet symmetric encryption** from the `cryptography` library.

### Key Features:
- ✅ Automatic encryption when tokens are saved to DB
- ✅ Automatic decryption when tokens are retrieved for API calls
- ✅ Transparent to the rest of the codebase
- ✅ Backward compatible with existing code

## Setup Instructions

### Step 1: Generate Encryption Key

Run the Django management command to generate a new encryption key:

```bash
python manage.py generate_encryption_key
```

Output:
```
✅ New Encryption Key Generated:

gAAAAABlXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX...

📝 Add this to your .env file:

ENCRYPTION_KEY=gAAAAABlXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX...

⚠️  Important:
- Keep this key SECRET and secure
- Do NOT commit to version control
- Use the same key across all servers
- Do NOT change this key after encrypting tokens
```

### Step 2: Add to .env File

Copy the generated key and add it to your `.env` file:

```env
ENCRYPTION_KEY=gAAAAABlXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
```

### Step 3: Verify Configuration

Test that encryption/decryption works:

```bash
python test_encryption.py
```

Expected output:
```
============================================================
Token Encryption Test
============================================================

[TEST 1] Checking ENCRYPTION_KEY configuration...
✅ ENCRYPTION_KEY is configured

[TEST 2] Testing token encryption...
✅ Encryption successful

[TEST 3] Testing token decryption...
✅ Decryption successful
✅ Decrypted token matches original!

...

✅ All tests passed!
```

### Step 4: Test with Model

Test that model encryption/decryption works correctly:

```bash
python test_model_encryption.py
```

Expected output:
```
============================================================
Model Encryption Test
============================================================

[TEST 1] Creating test page with plaintext token...
✅ Page instance created (not saved yet)

[TEST 2] Saving page to DB (token should be encrypted)...
✅ Page saved successfully

[TEST 3] Retrieving page from DB...
✅ Page retrieved from DB
✅ Token is encrypted (starts with gAAAAAB)

[TEST 4] Decrypting token using get_decrypted_token()...
✅ Token decrypted successfully
✅ Decrypted token matches original!

...

✅ All model encryption tests passed!
```

## How It Works

### Automatic Encryption (on save)

When you save a `ManagedFacebookPage` with a plaintext token:

```python
from video_management.models import ManagedFacebookPage

page = ManagedFacebookPage.objects.create(
    page_id="123456789",
    name="My Page",
    page_access_token="EAAY1XXXXXXXXXXXXXXXXXXX",  # Plaintext from Facebook
)
# Automatically encrypted and stored in DB!
```

Behind the scenes:
1. `ManagedFacebookPage.save()` is called
2. The overridden `save()` method detects plaintext token (doesn't start with `gAAAAAB`)
3. Token is encrypted using `TokenEncryption.encrypt()`
4. Encrypted token is stored in DB

### Automatic Decryption (on retrieval)

When you need the token for API calls:

```python
from video_management.models import ManagedFacebookPage

page = ManagedFacebookPage.objects.get(page_id="123456789")

# Get decrypted token
decrypted_token = page.get_decrypted_token()

# Use in FacebookGraphService
facebook_api.access_token = decrypted_token
```

The `get_decrypted_token()` method:
1. Checks if token is encrypted (starts with `gAAAAAB`)
2. If encrypted, decrypts using `TokenEncryption.decrypt()`
3. Returns plaintext token for use

### Where Tokens Are Decrypted

Tokens are automatically decrypted in these methods:

1. **`FacebookFetcher.refresh_all_pages_metrics()`**
   - Fetches current followers/likes for all pages
   - Used by background workers (Celery)
   
2. **`FacebookFetcher.sync_all_managed()`**
   - Syncs videos from all managed pages
   - Used by background workers

3. **API endpoints** (if called with page object)
   - Syncs specific page

## Encryption Details

### Library
- **Name**: `cryptography.fernet.Fernet`
- **Type**: Symmetric encryption
- **Algorithm**: AES 128 in CBC mode (via Fernet)
- **Encoding**: Base64

### Key Properties
- **Key Length**: 32 bytes (256 bits)
- **Key Format**: Base64-encoded
- **Example**: `gAAAAABlXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX`

### Encrypted Token Properties
- **Starts with**: `gAAAAAB` (Fernet prefix)
- **Length**: ~20% longer than original token
- **Example**: `gAAAAABlXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX`

## Important Notes

### ⚠️ Security Considerations

1. **Never commit ENCRYPTION_KEY to version control**
   - Add `ENCRYPTION_KEY=` to `.env.example` (without value)
   - Add `.env` to `.gitignore`

2. **Use the same key across all servers**
   - If you change servers/environments, use the same key
   - Tokens encrypted with one key cannot be decrypted with another

3. **Do NOT change the key after encrypting tokens**
   - Changing the key will make all existing tokens unreadable
   - If you must change the key:
     - Generate new tokens from Facebook
     - Delete old tokens
     - Use new key

4. **Keep backups of your encryption key**
   - Store safely (e.g., in secure password manager)
   - You'll need it if setting up new servers

### 🔄 Migration Path (if needed later)

If you need to change the encryption key:

```bash
# 1. Save old key
OLD_KEY=$(echo $ENCRYPTION_KEY)

# 2. Generate new key and set in .env
python manage.py generate_encryption_key

# 3. Update ENCRYPTION_KEY in .env
# ENCRYPTION_KEY=<new_key>

# 4. Re-import tokens from Facebook
# (This will re-encrypt with the new key)
```

## Troubleshooting

### Error: "ENCRYPTION_KEY not configured in settings"

**Solution**: 
1. Run `python manage.py generate_encryption_key`
2. Add the key to `.env` file
3. Restart Django

### Error: "Decryption failed"

**Possible causes**:
1. Token is corrupted
2. Using wrong encryption key
3. Token was encrypted with different key

**Solution**:
1. Verify ENCRYPTION_KEY in `.env`
2. Re-import the page from Facebook to get a new token

### Existing tokens in DB are now unreadable

**Cause**: Model encryption was added after tokens were already stored in plaintext

**Solution**:
1. Tokens will be automatically re-encrypted on save
2. Or manually re-import pages from Facebook

## Files Modified

- ✅ `video_management/models.py` - Added `save()` override and `get_decrypted_token()` method
- ✅ `video_management/utils/encryption.py` - Encryption utility class
- ✅ `video_management/services/facebook_fetcher.py` - Updated to use decrypted tokens
- ✅ `core/settings.py` - Added ENCRYPTION_KEY configuration
- ✅ `video_management/management/commands/generate_encryption_key.py` - Key generation command
- ✅ `test_encryption.py` - Encryption utility tests
- ✅ `test_model_encryption.py` - Model encryption tests

## Testing

### Run all encryption tests:

```bash
# Test encryption utilities
python test_encryption.py

# Test model encryption integration
python test_model_encryption.py
```

### Run Django tests:

```bash
python manage.py test video_management
```

## References

- [Cryptography Library Documentation](https://cryptography.io/)
- [Fernet Specification](https://cryptography.io/en/latest/fernet/)
- [Django Security Best Practices](https://docs.djangoproject.com/en/stable/topics/security/)
