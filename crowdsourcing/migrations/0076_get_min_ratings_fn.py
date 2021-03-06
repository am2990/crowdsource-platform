# -*- coding: utf-8 -*-
# Generated by Django 1.9 on 2015-12-20 19:18
from __future__ import unicode_literals

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('crowdsourcing', '0075_auto_20160429_1557'),
    ]

    operations = [
    	migrations.RunSQL('''
            CREATE OR REPLACE FUNCTION get_min_project_ratings()
              RETURNS TABLE(project_id INTEGER, owner_id INTEGER,  min_rating DOUBLE PRECISION)
            AS $$

			WITH project_seconds_worked AS (
			    SELECT p.id, COUNT(tw.id) * (60 * p.price / .1) project_seconds
			    FROM crowdsourcing_project p
			    INNER JOIN crowdsourcing_task t ON p.id=t.project_id
			    INNER JOIN crowdsourcing_taskworker tw ON t.id=tw.task_id
			    WHERE tw.task_status=2 AND EXTRACT('EPOCH' FROM (NOW() - tw.last_updated)) <= EXTRACT('EPOCH' FROM INTERVAL '1 hour')
			    AND p.deadline IS NULL OR p.deadline > NOW()
			    GROUP BY p.id
			), potential_project_seconds_worked AS (
			    SELECT p.id, p.owner_id,  p.min_rating, (COUNT(t.id) * p.repetition - COUNT(CASE WHEN tw.task_status IN (2, 3, 5) THEN 1 ELSE NULL END)) * (60 * p.price / .1) potential_project_seconds
			    FROM crowdsourcing_project p
			    INNER JOIN crowdsourcing_task t ON p.id=t.project_id
			    LEFT OUTER JOIN crowdsourcing_taskworker tw ON t.id=tw.task_id
			    WHERE p.status=3 AND p.deadline IS NULL OR p.deadline > NOW()
			    GROUP BY p.id
			)

			SELECT ppsw.id project_id, ppsw.owner_id,
			CASE WHEN sps.sum_project_seconds IS NULL THEN ppsw.min_rating
			WHEN spps.sum_potential_project_seconds=0 THEN ppsw.min_rating
			WHEN psw.project_seconds / sps.sum_project_seconds >= ppsw.potential_project_seconds / spps.sum_potential_project_seconds THEN ppsw.min_rating
			ELSE ppsw.min_rating * (1 - (ppsw.potential_project_seconds / spps.sum_potential_project_seconds - COALESCE(psw.project_seconds, 0) / sps.sum_project_seconds)) END min_rating
			FROM potential_project_seconds_worked ppsw
			LEFT OUTER JOIN project_seconds_worked psw ON ppsw.id=psw.id
			INNER JOIN (SELECT SUM(project_seconds) sum_project_seconds FROM project_seconds_worked) sps ON TRUE
			INNER JOIN (SELECT SUM(potential_project_seconds) sum_potential_project_seconds FROM potential_project_seconds_worked) spps ON TRUE

            $$
            LANGUAGE SQL
            STABLE
            RETURNS NULL ON NULL INPUT;
        ''')
    ]
