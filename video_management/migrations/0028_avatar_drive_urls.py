from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('video_management', '0027_alter_managedfacebookpage_page_access_token'),
    ]

    operations = [
        # ScrapedFanpage
        migrations.AddField(
            model_name='scrapedfanpage',
            name='avatar_drive_url',
            field=models.TextField(blank=True, default=''),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='scrapedfanpage',
            name='header_image_drive_url',
            field=models.TextField(blank=True, default=''),
            preserve_default=False,
        ),
        # DouyinProfile
        migrations.AddField(
            model_name='douyinprofile',
            name='avatar_drive_url',
            field=models.TextField(blank=True, default=''),
            preserve_default=False,
        ),
        # TikTokProfile
        migrations.AddField(
            model_name='tiktokprofile',
            name='avatar_drive_url',
            field=models.TextField(blank=True, default=''),
            preserve_default=False,
        ),
        # InstagramProfile
        migrations.AddField(
            model_name='instagramprofile',
            name='avatar_drive_url',
            field=models.TextField(blank=True, default=''),
            preserve_default=False,
        ),
        # ManagedFacebookPage
        migrations.AddField(
            model_name='managedfacebookpage',
            name='avatar_drive_url',
            field=models.TextField(blank=True, null=True),
        ),
    ]
