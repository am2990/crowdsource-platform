# -*- coding: utf-8 -*-
# Generated by Django 1.9 on 2017-05-19 15:53
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('crowdsourcing', '0162_auto_20170517_1729'),
    ]

    operations = [
        migrations.AddField(
            model_name='taskworker',
            name='approved_at',
            field=models.DateTimeField(null=True),
        ),
    ]
