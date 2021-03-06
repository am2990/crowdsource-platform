import json
from collections import OrderedDict
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.models import User
from django.db import connection, transaction
from django.db.models import F, Q
from django.utils import timezone
from ws4redis.publisher import RedisPublisher
from ws4redis.redis_store import RedisMessage

import constants
from crowdsourcing import models
from crowdsourcing.crypto import to_hash
from crowdsourcing.emails import send_notifications_email, send_new_tasks_email
from crowdsourcing.payment import Stripe
from crowdsourcing.redis import RedisProvider
from crowdsourcing.utils import hash_task
from csp.celery import app as celery_app
from mturk.tasks import get_provider


@celery_app.task(ignore_result=True)
def expire_tasks():
    cursor = connection.cursor()
    # noinspection SqlResolve
    query = '''
            WITH taskworkers AS (
                SELECT
                  tw.id,
                  p.id project_id
                FROM crowdsourcing_taskworker tw
                INNER JOIN crowdsourcing_task t ON  tw.task_id = t.id
                INNER JOIN crowdsourcing_project p ON t.project_id = p.id
                WHERE tw.created_at + coalesce(p.timeout, INTERVAL '24 hour') < NOW()
                AND tw.status=%(in_progress)s)
                UPDATE crowdsourcing_taskworker tw_up SET status=%(expired)s
            FROM taskworkers
            WHERE taskworkers.id=tw_up.id
            RETURNING tw_up.id, tw_up.worker_id
        '''
    cursor.execute(query,
                   {'in_progress': models.TaskWorker.STATUS_IN_PROGRESS, 'expired': models.TaskWorker.STATUS_EXPIRED})
    workers = cursor.fetchall()
    worker_list = []
    task_workers = []
    for w in workers:
        worker_list.append(w[1])
        task_workers.append({'id': w[0]})
    refund_task.delay(task_workers)
    update_worker_cache.delay(worker_list, constants.TASK_EXPIRED)
    return 'SUCCESS'


@celery_app.task(ignore_result=True)
def auto_approve_tasks():
    now = timezone.now()
    if now.weekday() in [5, 6]:
        return 'WEEKEND'
    if now.weekday() == 0 and now.hour < 15:
        return 'MONDAY'
    cursor = connection.cursor()

    # noinspection SqlResolve
    query = '''
        WITH taskworkers AS (
            SELECT
              tw.id,
              p.id project_id,
              p.group_id project_gid,
              tw.task_id,
              u.id user_id,
              u.username,
              u_worker.username worker_username
            FROM crowdsourcing_taskworker tw
            INNER JOIN crowdsourcing_task t ON  tw.task_id = t.id
            INNER JOIN crowdsourcing_project p ON t.project_id = p.id
            INNER JOIN auth_user u ON p.owner_id = u.id
            INNER JOIN auth_user u_worker ON tw.worker_id = u_worker.id
            WHERE tw.submitted_at + INTERVAL %(auto_approve_freq)s < NOW()
            AND tw.status=%(submitted)s)
            UPDATE crowdsourcing_taskworker tw_up SET status=%(accepted)s, approved_at = %(approved_at)s
        FROM taskworkers
        WHERE taskworkers.id=tw_up.id
        RETURNING tw_up.id, tw_up.worker_id, taskworkers.task_id, taskworkers.user_id, taskworkers.username,
        taskworkers.project_gid, taskworkers.worker_username
    '''
    cursor.execute(query,
                   {'submitted': models.TaskWorker.STATUS_SUBMITTED,
                    'accepted': models.TaskWorker.STATUS_ACCEPTED,
                    'approved_at': now,
                    'auto_approve_freq': '{} hour'.format(settings.AUTO_APPROVE_FREQ)})
    task_workers = cursor.fetchall()
    for w in task_workers:
        task_workers.append({'id': w[0]})
        post_approve.delay(w[2], 1)
        redis_publisher = RedisPublisher(facility='notifications', users=[w[4], w[6]])
        message = RedisMessage(
            json.dumps({"event": 'TASK_APPROVED', "project_gid": w[5], "project_key": to_hash(w[5])}))
        redis_publisher.publish_message(message)

    return 'SUCCESS'


@celery_app.task(ignore_result=True)
def update_worker_cache(workers, operation, key=None, value=None):
    provider = RedisProvider()

    for worker in workers:
        name = provider.build_key('worker', worker)
        if operation == constants.TASK_ACCEPTED:
            provider.hincrby(name, 'in_progress', 1)
        elif operation == constants.TASK_SUBMITTED:
            provider.hincrby(name, 'in_progress', -1)
            provider.hincrby(name, 'submitted', 1)
        elif operation == constants.TASK_REJECTED:
            provider.hincrby(name, 'submitted', -1)
            provider.hincrby(name, 'rejected', 1)
        elif operation == constants.TASK_RETURNED:
            provider.hincrby(name, 'submitted', -1)
            provider.hincrby(name, 'returned', 1)
        elif operation == constants.TASK_APPROVED:
            provider.hincrby(name, 'submitted', -1)
            provider.hincrby(name, 'approved', 1)
        elif operation in [constants.TASK_EXPIRED, constants.TASK_SKIPPED]:
            provider.hincrby(name, 'in_progress', -1)
        elif operation == constants.ACTION_GROUPADD:
            provider.set_add(name + ':worker_groups', value)
        elif operation == constants.ACTION_UPDATE_PROFILE:
            provider.set_hash(name, key, value)

    return 'SUCCESS'


@celery_app.task(ignore_result=True)
def email_notifications():
    users = User.objects.all()
    url = '%s/%s/' % (settings.SITE_HOST, 'messages')
    users_notified = []

    for user in users:
        email_notification, created = models.EmailNotification.objects.get_or_create(recipient=user)

        if created:
            # unread messages
            message_recipients = models.MessageRecipient.objects.filter(
                status__lt=models.MessageRecipient.STATUS_READ,
                recipient=user
            ).exclude(message__sender=user)

        else:
            # unread messages since last notification
            message_recipients = models.MessageRecipient.objects.filter(
                status__lt=models.MessageRecipient.STATUS_READ,
                created_at__gt=email_notification.updated_at,
                recipient=user
            ).exclude(message__sender=user)

        message_recipients = message_recipients.order_by('-created_at') \
            .select_related('message', 'recipient', 'message__sender') \
            .values('created_at', 'message__body', 'recipient__username', 'message__sender__username')

        result = OrderedDict()

        # group messages by sender
        for message_recipient in message_recipients:
            if message_recipient['message__sender__username'] in result:
                result[message_recipient['message__sender__username']].append(message_recipient)
            else:
                result[message_recipient['message__sender__username']] = [message_recipient]

        messages = [{'sender': k, 'messages': v} for k, v in result.items()]

        if len(messages) > 0:
            # send email
            send_notifications_email(email=user.email, url=url, messages=messages)

            users_notified.append(user)

    # update the last time user was notified
    models.EmailNotification.objects.filter(recipient__in=users_notified).update(updated_at=timezone.now())

    return 'SUCCESS'


@celery_app.task(bind=True, ignore_result=True)
def create_tasks(self, tasks):
    try:
        with transaction.atomic():
            task_obj = []
            x = 0
            for task in tasks:
                x += 1
                hash_digest = hash_task(task['data'])
                t = models.Task(data=task['data'], hash=hash_digest, project_id=task['project_id'],
                                row_number=x)
                task_obj.append(t)
            models.Task.objects.bulk_create(task_obj)
            models.Task.objects.filter(project_id=tasks[0]['project_id']).update(group_id=F('id'))
    except Exception as e:
        self.retry(countdown=4, exc=e, max_retries=2)

    return 'SUCCESS'


@celery_app.task(bind=True, ignore_result=True)
def create_tasks_for_project(self, project_id, file_deleted):
    project = models.Project.objects.filter(pk=project_id).first()
    if project is None:
        return 'NOOP'
    previous_rev = models.Project.objects.prefetch_related('batch_files', 'tasks').filter(~Q(id=project.id),
                                                                                          group_id=project.group_id) \
        .order_by('-id').first()

    previous_batch_file = previous_rev.batch_files.first() if previous_rev else None
    models.Task.objects.filter(project=project).delete()
    if file_deleted:
        models.Task.objects.filter(project=project).delete()
        task_data = {
            "project_id": project_id,
            "data": {}
        }
        task = models.Task.objects.create(**task_data)
        if previous_batch_file is None and previous_rev is not None:
            task.group_id = previous_rev.tasks.all().first().group_id
        else:
            task.group_id = task.id
        task.save()
        return 'SUCCESS'
    try:
        with transaction.atomic():
            data = project.batch_files.first().parse_csv()
            task_obj = []
            x = 0
            previous_tasks = previous_rev.tasks.all().order_by('row_number') if previous_batch_file else []
            previous_count = len(previous_tasks)
            for row in data:
                x += 1
                hash_digest = hash_task(row)
                price = None
                if project.allow_price_per_task and project.task_price_field is not None:
                    price = row.get(project.task_price_field)
                t = models.Task(data=row, hash=hash_digest, project_id=int(project_id), row_number=x, price=price)
                if previous_batch_file is not None and x <= previous_count:
                    if len(set(row.items()) ^ set(previous_tasks[x - 1].data.items())) == 0:
                        t.group_id = previous_tasks[x - 1].group_id
                task_obj.append(t)
            models.Task.objects.bulk_create(task_obj)
            models.Task.objects.filter(project_id=project_id, group_id__isnull=True) \
                .update(group_id=F('id'))
    except Exception as e:
        self.retry(countdown=4, exc=e, max_retries=2)

    return 'SUCCESS'


@celery_app.task(ignore_result=True)
def pay_workers():
    workers = User.objects.all()
    payment = Stripe()
    # total = 0
    #
    for worker in workers:
        task_workers = models.TaskWorker.objects.prefetch_related('task__project') \
            .filter(worker=worker,
                    status=models.TaskWorker.STATUS_ACCEPTED,
                    is_paid=False)
        for tw in task_workers:
            payment.pay_worker(tw)
            # total = sum(tasks.values_list('task__project__price', flat=True))
            # if total > 0 and worker.profile.paypal_email is not None and single_payout(total, worker):
            #     tasks.update(is_paid=True)
            #
            # return {"total": total}


def single_payout(amount, user):
    return 'OBSOLETE METHOD'
    # backend = PayPalBackend()
    #
    # payout = backend.paypalrestsdk.Payout({
    #     "sender_batch_header": {
    #         "sender_batch_id": "batch_worker_id__" + str(user.id) + '_week__' + str(timezone.now().isocalendar()[1]),
    #         "email_subject": "Daemo Payment"
    #     },
    #     "items": [
    #         {
    #             "recipient_type": "EMAIL",
    #             "amount": {
    #                 "value": amount,
    #                 "currency": "USD"
    #             },
    #             "receiver": user.profile.paypal_email,
    #             "note": "Your Daemo payment.",
    #             "sender_item_id": "item_1"
    #         }
    #     ]
    # })
    # payout_log = models.PayPalPayoutLog()
    # payout_log.worker = user
    # if payout.create(sync_mode=True):
    #     payout_log.is_valid = payout.batch_header.transaction_status == 'SUCCESS'
    #     payout_log.save()
    #     return payout_log.is_valid
    # else:
    #     payout_log.is_valid = False
    #     payout_log.response = payout.error
    #     payout_log.save()
    #     return False


@celery_app.task(ignore_result=True)
def post_approve(task_id, num_workers):
    task = models.Task.objects.prefetch_related('project').get(pk=task_id)
    latest_revision = models.Project.objects.filter(~Q(status=models.Project.STATUS_DRAFT),
                                                    group_id=task.project.group_id) \
        .order_by('-id').first()
    latest_revision.amount_due -= Decimal(num_workers * latest_revision.price)
    latest_revision.save()
    return 'SUCCESS'


def create_transaction(sender_id, recipient_id, amount, reference):
    return 'OBSOLETE METHOD'
    # transaction_data = {
    #     'sender_id': sender_id,
    #     'recipient_id': recipient_id,
    #     'amount': amount,
    #     'method': 'daemo',
    #     'sender_type': models.Transaction.TYPE_SYSTEM,
    #     'reference': 'P#' + str(reference)
    # }
    # with transaction.atomic():
    #     daemo_transaction = models.Transaction.objects.create(**transaction_data)
    #     daemo_transaction.recipient.balance += Decimal(daemo_transaction.amount)
    #     daemo_transaction.recipient.save()
    #     if daemo_transaction.sender.type not in [models.FinancialAccount.TYPE_WORKER,
    #                                              models.FinancialAccount.TYPE_REQUESTER]:
    #         daemo_transaction.sender.balance -= Decimal(daemo_transaction.amount)
    #         daemo_transaction.sender.save()
    # return 'SUCCESS'


@celery_app.task(ignore_result=True)
def refund_task(task_worker_in):
    return 'OBSOLETE METHOD'
    # task_worker_ids = [tw['id'] for tw in task_worker_in]
    # system_account = models.FinancialAccount.objects.get(is_system=True,
    #                                                      type=models.FinancialAccount.TYPE_ESCROW).id
    # task_workers = models.TaskWorker.objects.prefetch_related('task', 'task__project').filter(
    #     id__in=task_worker_ids)
    # amount = 0
    # for task_worker in task_workers:
    #
    #     latest_revision = models.Project.objects.filter(~Q(status=models.Project.STATUS_DRAFT),
    #                                                     group_id=task_worker.task.project.group_id) \
    #         .order_by('-id').first()
    #     is_running = latest_revision.deadline is None or latest_revision.deadline > timezone.now()
    #     if task_worker.task.project_id == latest_revision.id:
    #         amount = 0
    #     elif task_worker.task.exclude_at is not None:
    #         amount = task_worker.task.project.price
    #     elif is_running and latest_revision.price >= task_worker.task.project.price:
    #         amount = 0
    #     elif is_running and latest_revision.price < task_worker.task.project.price:
    #         amount = task_worker.task.project.price - latest_revision.price
    #     else:
    #         amount = latest_revision.price
    #     if amount > 0:
    #         requester_account = models.FinancialAccount.objects.get(owner_id=task_worker.task.project.owner_id,
    #                                                                 type=models.FinancialAccount.TYPE_REQUESTER,
    #                                                                 is_system=False).id
    #         create_transaction(sender_id=system_account, recipient_id=requester_account, amount=amount,
    #                            reference=task_worker.id)
    #         latest_revision.amount_due -= Decimal(amount)
    #         latest_revision.save()
    # return 'SUCCESS'


@celery_app.task(ignore_result=True)
def update_feed_boomerang():
    # TODO fix group_id
    cursor = connection.cursor()
    # noinspection SqlResolve
    query = '''
        WITH boomerang_ratings AS (
            SELECT
                pid, min_rating, tasks_in_progress, task_count,
                CASE
                    WHEN task_count > 0
                        AND (
                            (
                                tasks_in_progress > 0
                                AND task_count/tasks_in_progress >= (%(BOOMERANG_LAMBDA)s)
                            )
                            OR tasks_in_progress = 0
                        )
                        THEN min_rating
                    WHEN avg_worker_rating <= (%(BOOMERANG_MIDPOINT)s)
                        AND min_rating>(%(BOOMERANG_MIDPOINT)s)
                        THEN (%(BOOMERANG_MIDPOINT)s)
                    ELSE
                        avg_worker_rating
                END new_min_rating
            FROM (
                SELECT t.pid, t.min_rating, t.tasks_in_progress, t.task_count,
                        max(t.avg_worker_rating) avg_worker_rating
                FROM (
                    SELECT
                        p.id pid,
                        p.min_rating,
                        p.tasks_in_progress,
                        t.task_count,
                        round(coalesce(m.task_w_avg, (%(BOOMERANG_MIDPOINT)s))::NUMERIC, 2) avg_worker_rating
                    FROM
                        crowdsourcing_project p

                    INNER JOIN (
                        SELECT
                            p1.group_id  pgid,
                            count(tw.id) task_count
                        FROM
                            crowdsourcing_task t
                        INNER JOIN
                            crowdsourcing_project p1
                            ON
                                t.project_id = p1.id
                        LEFT OUTER JOIN
                            crowdsourcing_taskworker tw
                            ON
                                t.id = tw.task_id
                                AND tw.status IN (1, 2, 3, 5)
                                AND tw.created_at BETWEEN now() - ((%(HEART_BEAT_BOOMERANG)s) ||' minute')::INTERVAL
                                AND now()
                        GROUP BY p1.group_id
                    ) t
                    ON
                        t.pgid = p.group_id

                    LEFT OUTER JOIN (
                        SELECT
                            target_id,
                            username,
                            sum(weight * power((%(BOOMERANG_PLATFORM_ALPHA)s), r.row_number))
                                / sum(power((%(BOOMERANG_PLATFORM_ALPHA)s), r.row_number)) platform_w_avg
                        FROM (
                            SELECT
                                r.id,
                                u.username                        username,
                                weight,
                                r.target_id,
                                -1 + row_number()
                            OVER (
                                PARTITION BY target_id
                                ORDER BY tw.created_at DESC
                            ) AS row_number
                            FROM
                                crowdsourcing_rating r

                            INNER JOIN
                                crowdsourcing_task t
                                ON
                                    t.id = r.task_id
                            INNER JOIN
                                crowdsourcing_taskworker tw
                                ON
                                    t.id = tw.task_id
                                    AND tw.worker_id=r.target_id
                            INNER JOIN
                                auth_user u
                                ON
                                    u.id = r.target_id
                            WHERE
                                origin_type = (%(origin_type)s)
                        ) r
                        GROUP BY target_id, username
                    ) m_platform
                        ON TRUE
                        --ON m_platform.platform_w_avg < p.min_rating

                    LEFT OUTER JOIN (

                        SELECT
                            target_id,
                            origin_id,
                            sum(weight * power((%(BOOMERANG_REQUESTER_ALPHA)s), t.row_number))
                                / sum(power((%(BOOMERANG_REQUESTER_ALPHA)s), t.row_number)) requester_w_avg
                        FROM (

                            SELECT
                                r.id,
                                r.origin_id,
                                weight,
                                r.target_id,
                                -1 + row_number()
                                OVER (
                                    PARTITION BY target_id
                                    ORDER BY tw.created_at DESC
                                ) AS row_number
                            FROM
                                crowdsourcing_rating r
                            INNER JOIN
                                crowdsourcing_task t
                                ON
                                    t.id = r.task_id
                            INNER JOIN
                                crowdsourcing_taskworker tw
                                ON
                                    t.id = tw.task_id
                                    AND tw.worker_id=r.target_id
                            WHERE
                                origin_type = (%(origin_type)s)
                        ) t
                        GROUP BY origin_id, target_id
                    ) mp
                    ON
                        mp.origin_id = p.owner_id
                        AND mp.target_id = m_platform.target_id
                        ---AND mp.requester_w_avg < p.min_rating

                    LEFT OUTER JOIN (
                        SELECT
                            target_id,
                            origin_id,
                            project_id,
                            sum(weight * power((%(BOOMERANG_TASK_ALPHA)s), t.row_number))
                                / sum(power((%(BOOMERANG_TASK_ALPHA)s), t.row_number)) task_w_avg
                        FROM (

                            SELECT
                                r.id,
                                r.origin_id,
                                p.id                              project_id,
                                weight,
                                r.target_id,
                                -1 + row_number()
                            OVER (
                                PARTITION BY target_id
                                ORDER BY tw.created_at DESC
                            ) AS row_number
                            FROM
                                crowdsourcing_rating r
                            INNER JOIN
                                crowdsourcing_task t
                                ON
                                    t.id = r.task_id
                            INNER JOIN
                                crowdsourcing_project p
                                ON
                                    p.id = t.project_id
                            INNER JOIN
                                crowdsourcing_taskworker tw
                                ON
                                    t.id = tw.task_id
                                    AND tw.worker_id=r.target_id
                            WHERE
                                origin_type = (%(origin_type)s)
                        ) t
                        GROUP BY origin_id, target_id, project_id
                    )m
                    ON
                        m.origin_id = p.owner_id
                        AND p.id = m.project_id
                        AND m.target_id = mp.target_id
                        --AND m.task_w_avg < p.min_rating

                    INNER JOIN (

                        SELECT
                            group_id,
                            max(id) max_id
                        FROM
                            crowdsourcing_project
                        WHERE
                            status = (%(in_progress)s)
                            AND deleted_at IS NULL
                        GROUP BY group_id
                    ) most_recent
                    ON
                        most_recent.max_id = p.id

                    WHERE
            p.rating_updated_at < now() + ('4 second')::INTERVAL -((%(HEART_BEAT_BOOMERANG)s) ||' minute')::INTERVAL
                        AND p.min_rating > 0
                ) t
                WHERE
                    t.avg_worker_rating < t.min_rating
                GROUP BY t.pid, t.min_rating, t.task_count, t.tasks_in_progress
            ) combined
        )

        UPDATE
            crowdsourcing_project p
        SET
            min_rating = boomerang_ratings.new_min_rating,
            rating_updated_at = now(),
            tasks_in_progress =
                CASE
                    WHEN
                        boomerang_ratings.new_min_rating <> p.min_rating
                        OR (
                            boomerang_ratings.new_min_rating = p.min_rating
                            AND boomerang_ratings.task_count > boomerang_ratings.tasks_in_progress
                        )
                    THEN
                        boomerang_ratings.task_count
                    ELSE
                        boomerang_ratings.tasks_in_progress
                END,
            previous_min_rating = boomerang_ratings.min_rating
        FROM
            boomerang_ratings
        WHERE
            boomerang_ratings.pid = p.id
        RETURNING
            p.id, p.group_id, p.min_rating, p.rating_updated_at
    '''

    #
    #
    #
    task_boomerang_query = '''
        WITH boomerang_ratings AS (
            SELECT
                tid,
                min_rating,
                CASE
                -- Force boomerang to mid-point to prioritize new workers (default mid-point) over poorly rated ones
                -- if we have remaining known mturk workers below mid-point
                    WHEN
                        avg_worker_rating <= (%(BOOMERANG_MIDPOINT)s)
                        AND min_rating > (%(BOOMERANG_MIDPOINT)s)
                    THEN
                        (%(BOOMERANG_MIDPOINT)s)
                    ELSE
                        avg_worker_rating
                END new_min_rating
            FROM (
                SELECT
                    tid,
                    min_rating,
                    min(avg_worker_rating) avg_worker_rating
                FROM (
                    SELECT
                        p.pid,
                        t.id                    tid,
                        t.min_rating,
                        p.avg_worker_rating     avg_worker_rating,
                        row_number()
                            OVER (PARTITION BY t.id ORDER BY p.avg_worker_rating DESC) row_number
                    FROM (
                        SELECT
                            p.id pid,
                            p.min_rating,
                            -- mp.requester_w_avg, m_platform.platform_w_avg,
                            round(coalesce(m.task_w_avg, (%(BOOMERANG_MIDPOINT)s)) :: NUMERIC, 2) avg_worker_rating
                        FROM
                            crowdsourcing_project p

                        LEFT OUTER JOIN (

                            -- Get platform ratings for all workers
                            -- r (origin_type) => (target_id, username, platform_w_avg)

                            SELECT
                                target_id,
                                username,
                                sum(weight * power((%(BOOMERANG_PLATFORM_ALPHA)s), r.row_number))
                                    / sum(power((%(BOOMERANG_PLATFORM_ALPHA)s), r.row_number)) platform_w_avg
                            FROM (

                                -- Get all ratings for workers for tasks in reverse chronological order (recent first)
                                -- (origin_type) => (id, username, weight, target_id, row_number)

                                SELECT
                                    r.id,
                                    u.username username,
                                    weight,
                                    r.target_id,
                                    -1 + row_number()
                                        OVER (PARTITION BY target_id ORDER BY tw.created_at DESC) AS row_number
                                FROM
                                    crowdsourcing_rating r

                                INNER JOIN crowdsourcing_task t
                                    ON t.id = r.task_id
                                INNER JOIN crowdsourcing_taskworker tw
                                    ON
                                        t.id = tw.task_id
                                        AND tw.worker_id=r.target_id
                                INNER JOIN auth_user u ON u.id = r.target_id

                                WHERE
                                    origin_type = (%(origin_type)s)
                            ) r
                            GROUP BY target_id, username
                        ) m_platform
                            ON TRUE

                        LEFT OUTER JOIN (

                            -- Get requester provided avg ratings for all workers
                            -- (req_alpha, origin_type) => (target_id, origin_id, requester_w_avg)

                            SELECT
                                target_id,
                                origin_id,
                                sum(weight * power((%(BOOMERANG_REQUESTER_ALPHA)s), t.row_number))
                                    / sum(power((%(BOOMERANG_REQUESTER_ALPHA)s), t.row_number)) requester_w_avg
                            FROM (

                                -- Get ratings for all workers for tasks (recent first)
                                -- r (origin_type) => (id, origin_id, weight, target_id, row_number)

                                SELECT
                                    r.id,
                                    r.origin_id,
                                    weight,
                                    r.target_id,
                                    -1 + row_number()
                                        OVER (PARTITION BY target_id ORDER BY tw.created_at DESC) AS row_number
                                FROM
                                    crowdsourcing_rating r

                                INNER JOIN crowdsourcing_task t ON t.id = r.task_id

                                INNER JOIN crowdsourcing_taskworker tw
                                    ON
                                        t.id = tw.task_id
                                        AND tw.worker_id=r.target_id
                                WHERE
                                    origin_type = (%(origin_type)s)) t
                                GROUP BY origin_id, target_id
                        ) mp
                            ON
                                mp.origin_id = p.owner_id
                                AND mp.target_id = m_platform.target_id

                        LEFT OUTER JOIN (

                            -- Get project specific avg ratings for all workers
                            -- (task_alpha, origin_type) => (target_id, origin_id, project_id, task_w_avg)

                            SELECT
                                target_id,
                                origin_id,
                                project_id,
                                sum(weight * power((%(BOOMERANG_TASK_ALPHA)s), t.row_number))
                                    / sum(power((%(BOOMERANG_TASK_ALPHA)s), t.row_number)) task_w_avg
                            FROM (

                                SELECT
                                    r.id,
                                    r.origin_id,
                                    p.id project_id,
                                    weight,
                                    r.target_id,
                                    -1 + row_number()
                                        OVER (PARTITION BY target_id ORDER BY tw.created_at DESC) AS row_number
                                FROM
                                    crowdsourcing_rating r
                                INNER JOIN crowdsourcing_task t ON t.id = r.task_id
                                INNER JOIN crowdsourcing_project p ON p.id = t.project_id
                                INNER JOIN crowdsourcing_taskworker tw
                                    ON
                                        t.id = tw.task_id
                                        AND tw.worker_id=r.target_id
                                WHERE origin_type = (%(origin_type)s)
                            ) t
                            GROUP BY origin_id, target_id, project_id
                        ) m
                            ON
                                m.origin_id = p.owner_id
                                AND p.id = m.project_id
                                AND m.target_id = mp.target_id

                        INNER JOIN (
                            SELECT
                                group_id,
                                max(id) max_id
                            FROM
                                crowdsourcing_project
                            WHERE
                                status = (%(in_progress)s)
                                AND deleted_at IS NULL
                            GROUP BY group_id
                        ) most_recent
                            ON
                                most_recent.max_id = p.id

                        WHERE
                            p.min_rating > 0
                            AND (p.min_rating <> p.previous_min_rating OR p.min_rating = (%(BOOMERANG_MAX)s))
                    ) p

                    INNER JOIN crowdsourcing_task t
                        ON t.project_id = p.pid

                    INNER JOIN (
                        SELECT
                            max(id)  id,
                            repetition,
                            group_id,
                            repetition - sum(existing_assignments) remaining_assignments
                        FROM (
                            SELECT
                                t_rev.id,
                                t.group_id,
                                p.repetition,
                                CASE
                                    WHEN
                                        tw.id IS NULL
                                        OR tw.status IN ((%(skipped)s), (%(expired)s), (%(rejected)s))
                                    THEN 0
                                    ELSE 1
                                END existing_assignments
                            FROM
                                crowdsourcing_task t

                            INNER JOIN crowdsourcing_project p
                                ON t.project_id = p.id

                            INNER JOIN crowdsourcing_task t_rev
                                ON t_rev.group_id = t.group_id

                            LEFT OUTER JOIN crowdsourcing_taskworker tw
                                ON
                                    tw.task_id = t_rev.id
                                    AND t_rev.exclude_at IS NULL
                                    AND t_rev.deleted_at IS NULL
                        ) t
                        GROUP BY group_id, repetition
                        HAVING sum(existing_assignments) < repetition
                    ) t_remaining
                        ON t_remaining.id = t.id

                    WHERE
                        p.avg_worker_rating < t.min_rating
                        -- AND p.row_number < (%(BOOMERANG_WORKERS_NEEDED)s)
                ) combined

                WHERE
                    row_number < (%(BOOMERANG_WORKERS_NEEDED)s)
                GROUP BY tid, min_rating
            ) ranked
        )

        UPDATE
            crowdsourcing_task t
        SET
            min_rating = boomerang_ratings.new_min_rating,
            rating_updated_at = now()
        FROM
            boomerang_ratings
        WHERE
            boomerang_ratings.tid = t.id
        RETURNING
            t.id, t.group_id, t.min_rating, t.rating_updated_at;
    '''

    # get all workers and their project ratings
    # filter the ones who have not done any particular task ever
    # filter the ones who have at least new min boomerang rating
    # noinspection SqlResolve
    email_query = '''
        SELECT
          p.id,
          p.group_id,
          owner_profile.handle,
          t_count.worker_id,
          sum(available) available_count,
          u.email,
          p.name,
          p.price
        FROM crowdsourcing_task t INNER JOIN (SELECT
                                                group_id,
                                                max(id) id
                                              FROM crowdsourcing_task
                                              WHERE deleted_at IS NULL
                                              GROUP BY group_id) t_max ON t_max.id = t.id
          INNER JOIN crowdsourcing_project p ON p.id = t.project_id
          INNER JOIN (
                       SELECT
                         t.group_id,
                         t.worker_id,
                         1 available,
                         sum(t.done) done
                       FROM (
                              SELECT
                                t.group_id,
                                u_all.id   worker_id,
                                CASE WHEN (tw.worker_id IS NOT NULL)
                                          AND tw.status NOT IN (4, 6, 7)
                                  THEN 1
                                ELSE 0 END done
                              FROM crowdsourcing_task t
                                INNER JOIN auth_user u_all ON TRUE
                                INNER JOIN crowdsourcing_userprofile profile_all ON profile_all.user_id = u_all.id
                                LEFT OUTER JOIN crowdsourcing_taskworker tw ON (t.id = tw.task_id
                                AND tw.worker_id = u_all.id)
                              WHERE t.exclude_at IS NULL AND t.deleted_at IS NULL AND profile_all.is_worker = TRUE) t
                       GROUP BY t.group_id, t.worker_id)
                     t_count ON t_count.group_id = t.group_id
          INNER JOIN get_min_project_ratings() ratings ON ratings.project_id = p.id
          LEFT OUTER JOIN get_worker_ratings(worker_id) worker_ratings
            ON worker_ratings.requester_id = p.owner_id
             AND coalesce(worker_ratings.worker_rating, 1.99) >= ratings.min_rating
          LEFT OUTER JOIN crowdsourcing_WorkerProjectNotification n
            ON n.project_id = p.group_id AND n.worker_id = t_count.worker_id
          INNER JOIN auth_user u ON u.id = t_count.worker_id
          INNER JOIN crowdsourcing_userpreferences pref ON pref.user_id = u.id
          INNER JOIN auth_user owner ON owner.id = p.owner_id
          INNER JOIN crowdsourcing_userprofile owner_profile ON owner_profile.user_id = owner.id
          LEFT OUTER JOIN (
                            SELECT
                              p.id,
                              tw.worker_id,
                              count(tw.id) tasks_done
                            FROM crowdsourcing_project p
                              INNER JOIN crowdsourcing_task t ON p.id = t.project_id
                              LEFT OUTER JOIN crowdsourcing_taskworker tw ON tw.task_id = t.id
                            GROUP BY p.id, tw.worker_id
                          ) worker_project ON worker_project.id = p.id AND worker_project.worker_id = t_count.worker_id
        WHERE t_count.done < p.repetition
              AND p.status = 3 AND n.id IS NULL AND pref.new_tasks_notifications = TRUE
              AND coalesce(worker_project.tasks_done, 0) = 0 --and worker_ratings.worker_rating is not null
        GROUP BY p.id, p.group_id, owner_profile.handle, t_count.worker_id, u.email, p.name, p.price
        HAVING t_count.worker_id IS NOT NULL
        ORDER BY 1 DESC;
    '''
    params = {
        'in_progress': models.Project.STATUS_IN_PROGRESS,
        'HEART_BEAT_BOOMERANG': settings.HEART_BEAT_BOOMERANG,
        'BOOMERANG_TASK_ALPHA': settings.BOOMERANG_TASK_ALPHA,
        'BOOMERANG_REQUESTER_ALPHA': settings.BOOMERANG_REQUESTER_ALPHA,
        'BOOMERANG_PLATFORM_ALPHA': settings.BOOMERANG_PLATFORM_ALPHA,
        'BOOMERANG_MIDPOINT': settings.BOOMERANG_MIDPOINT,
        'BOOMERANG_LAMBDA': settings.BOOMERANG_LAMBDA,
        'origin_type': models.Rating.RATING_REQUESTER
    }

    cursor.execute(query, params)
    projects = cursor.fetchall()

    tasks = []

    # if cursor.rowcount > 0:
    params.update({
        'skipped': models.TaskWorker.STATUS_SKIPPED,
        'rejected': models.TaskWorker.STATUS_REJECTED,
        'expired': models.TaskWorker.STATUS_EXPIRED,
        'BOOMERANG_MAX': settings.BOOMERANG_MAX,
        'BOOMERANG_WORKERS_NEEDED': settings.BOOMERANG_WORKERS_NEEDED
    })
    cursor.execute(task_boomerang_query, params)
    tasks = cursor.fetchall()

    try:
        cursor.execute(email_query, {})
        workers = cursor.fetchall()
        worker_project_notifications = []
        for worker in workers:
            try:
                send_new_tasks_email(to=worker[5], project_id=worker[0],
                                     project_name=worker[6], price=worker[7],
                                     available_tasks=worker[4], requester_handle=worker[2])
                worker_project_notifications.append(models.WorkerProjectNotification(project_id=worker[1],
                                                                                     worker_id=worker[3]))
            except Exception as e:
                print(e)
        models.WorkerProjectNotification.objects.bulk_create(worker_project_notifications)
        # cursor.execute(worker_notification_query, params)
        # workers = cursor.fetchall()
        #
        # for worker in workers:
        #     # user_id = worker[0]
        #     username = worker[1]
        #     mturk_id = (username.split('.')[1]).upper()
        #     mturk_worker_ids = [mturk_id]
        #     project_id = worker[2]
        #     project_name = worker[3]
        #     subject = "New HITs for %s posted for you on MTurk" % project_name
        #     message = "Hello, \n" \
        #               "Due to your recent work on the project %s on Mechanical Turk, " \
        #               "you've qualified to work on some new HITs available only to you for the same project.\n " \
        #               "We would really appreciate if you participate again.\n " \
        #               "Thank you in advance." % project_name
        #
        #     notify_workers.delay(project_id, mturk_worker_ids, subject, message)
    except:
        pass

    logs = []

    for project in projects:
        logs.append(models.BoomerangLog(object_id=project[1], min_rating=project[2], rating_updated_at=project[3],
                                        reason='DEFAULT'))

    for task in tasks:
        logs.append(models.BoomerangLog(object_id=task[1], min_rating=task[2], object_type='task',
                                        rating_updated_at=task[3],
                                        reason='DEFAULT'))

    models.BoomerangLog.objects.bulk_create(logs)

    return 'SUCCESS: {} rows affected'.format(cursor.rowcount)


@celery_app.task(ignore_result=True)
def update_project_boomerang(project_id):
    project = models.Project.objects.filter(pk=project_id).first()
    if project is not None:
        project.min_rating = 3.0
        # project.rating_updated_at = timezone.now()
        project.save()
        models.BoomerangLog.objects.create(object_id=project.group_id, min_rating=project.min_rating,
                                           rating_updated_at=project.rating_updated_at, reason='RESET')
    return 'SUCCESS'


@celery_app.task
def background_task(function, **kwargs):
    function(**kwargs)
    return 'SUCCESS'


# Payment Tasks
@celery_app.task(ignore_result=True)
def create_account_and_customer(user_id, ip_address):
    from crowdsourcing.payment import Stripe
    try:
        user = User.objects.get(pk=user_id)
        Stripe().create_account_and_customer(user=user, country_iso=user.profile.address.city.country.code,
                                             ip_address=ip_address)
    except User.DoesNotExist:
        return 'User does not exist'
    return 'SUCCESS'


@celery_app.task(ignore_result=True)
def refund_charges_before_expiration():
    from crowdsourcing.payment import Stripe
    charges = models.StripeCharge.objects.filter(expired=False, balance__gt=50,
                                                 created_at__gt=timezone.now() - settings.STRIPE_CHARGE_LIFETIME)

    for charge in charges:
        try:
            Stripe().refund(charge=charge, amount=charge.balance)
            charge.expired = True
            charge.expired_at = timezone.now()
            charge.save()
        except Exception:
            pass


@celery_app.task(ignore_result=True)
def notify_workers(project_id, worker_ids, subject, message):
    project = models.Project.objects.values('owner').get(id=project_id)

    user = User.objects.get(id=project['owner'])
    provider = get_provider(user)

    if provider is None:
        return

    provider.notify_workers(worker_ids=worker_ids, subject=subject, message_text=message)
    return 'SUCCESS'
