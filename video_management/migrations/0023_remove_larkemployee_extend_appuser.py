# users table owned by Prisma — chỉ cập nhật Django state, không ALTER DB.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('video_management', '0022_reportsettings'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddField(
                    model_name='appuser',
                    name='team',
                    field=models.CharField(blank=True, max_length=255, null=True),
                ),
                migrations.AddField(
                    model_name='appuser',
                    name='image_url',
                    field=models.TextField(blank=True, null=True),
                ),
                migrations.AddField(
                    model_name='appuser',
                    name='employee_data',
                    field=models.JSONField(blank=True, null=True),
                ),
                migrations.AddField(
                    model_name='appuser',
                    name='lark_employee_record_id',
                    field=models.CharField(blank=True, max_length=255, null=True),
                ),
                migrations.AddField(
                    model_name='appuser',
                    name='employee_id',
                    field=models.CharField(blank=True, max_length=255, null=True),
                ),
                migrations.AddField(
                    model_name='appuser',
                    name='employee_position',
                    field=models.CharField(blank=True, max_length=255, null=True),
                ),
                migrations.AddField(
                    model_name='appuser',
                    name='employee_status',
                    field=models.CharField(blank=True, max_length=255, null=True),
                ),
                migrations.AddField(
                    model_name='appuser',
                    name='employee_date',
                    field=models.DateTimeField(blank=True, null=True),
                ),
                migrations.DeleteModel(name='LarkEmployee'),
            ],
            database_operations=[],
        ),
    ]
