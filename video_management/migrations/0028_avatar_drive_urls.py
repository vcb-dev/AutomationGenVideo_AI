from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('video_management', '0027_alter_managedfacebookpage_page_access_token'),
    ]

    operations = [
        # Scraper models (ScrapedFanpage, DouyinProfile, etc.) are managed=False
        # and owned by BE/Prisma — do not alter them via Django migrations.
        migrations.AddField(
            model_name='managedfacebookpage',
            name='avatar_drive_url',
            field=models.TextField(blank=True, null=True),
        ),
    ]
