# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('newsletter', '0003_auto_20160226_1518'),
    ]

    operations = [
        migrations.CreateModel(
            name='Bounce',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('date_create', models.DateTimeField(auto_now_add=True, verbose_name='created')),
                ('hard', models.BooleanField(default=False, verbose_name='hard bounce')),
                ('status_code', models.CharField(max_length=9, verbose_name='status code')),
                ('content', models.TextField(verbose_name='content')),
                ('subscription', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='newsletter.Subscription', verbose_name='subscription')),
            ],
            options={
                'verbose_name': 'bounce',
                'verbose_name_plural': 'bounces',
            },
        ),
    ]
