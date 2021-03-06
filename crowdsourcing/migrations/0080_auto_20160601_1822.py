# -*- coding: utf-8 -*-
# Generated by Django 1.9 on 2016-06-01 18:22
from __future__ import unicode_literals

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('crowdsourcing', '0079_merge'),
    ]

    operations = [
        migrations.AlterField(
            model_name='projectcomment',
            name='project',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='comments', to='crowdsourcing.Project'),
        ),
        migrations.AlterField(
            model_name='taskcomment',
            name='task',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='comments', to='crowdsourcing.Task'),
        ),
    ]
