from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('video_management', '0027_alter_managedfacebookpage_page_access_token'),
    ]

    operations = [
        # ManagedFacebookPage
        migrations.AddField(
            model_name='managedfacebookpage',
            name='avatar_drive_url',
            field=models.TextField(blank=True, null=True),
        ),
    ]
