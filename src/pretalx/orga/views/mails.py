from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.utils.functional import cached_property
from django.utils.translation import gettext_lazy as _
from django.views.generic import FormView, ListView, TemplateView, View
from django_context_decorator import context

from pretalx.common.mixins.views import (
    ActionFromUrl, EventPermissionRequired, Filterable, PermissionRequired, Sortable,
)
from pretalx.common.views import CreateOrUpdateView
from pretalx.mail.context import get_context_explanation
from pretalx.mail.models import MailTemplate, QueuedMail
from pretalx.orga.forms.mails import MailDetailForm, MailTemplateForm, WriteMailForm
from pretalx.person.models import User


class OutboxList(EventPermissionRequired, Sortable, Filterable, ListView):
    model = QueuedMail
    context_object_name = 'mails'
    template_name = 'orga/mails/outbox_list.html'
    default_filters = ('to__icontains', 'subject__icontains')
    filterable_fields = ('to', 'subject')
    sortable_fields = ('to', 'subject')
    paginate_by = 25
    permission_required = 'orga.view_mails'

    def get_queryset(self):
        qs = self.request.event.queued_mails.prefetch_related('to_users').filter(sent__isnull=True).order_by('-id')
        qs = self.filter_queryset(qs)
        qs = self.sort_queryset(qs)
        return qs


class SentMail(EventPermissionRequired, Sortable, Filterable, ListView):
    model = QueuedMail
    context_object_name = 'mails'
    template_name = 'orga/mails/sent_list.html'
    default_filters = ('to__icontains', 'subject__icontains')
    filterable_fields = ('to', 'subject')
    sortable_fields = ('to', 'subject', 'sent')
    paginate_by = 25
    permission_required = 'orga.view_mails'

    def get_queryset(self):
        qs = self.request.event.queued_mails.prefetch_related('to_users').filter(sent__isnull=False).order_by(
            '-sent'
        )
        qs = self.filter_queryset(qs)
        qs = self.sort_queryset(qs)
        return qs


class OutboxSend(EventPermissionRequired, TemplateView):
    permission_required = 'orga.send_mails'
    template_name = 'orga/mails/confirm.html'

    @context
    def question(self):
        return _('Do you really want to send {count} mails?').format(
            count=self.queryset.count()
        )

    def dispatch(self, request, *args, **kwargs):
        if 'pk' in self.kwargs:
            try:
                mail = self.request.event.queued_mails.get(pk=self.kwargs.get('pk'))
            except QueuedMail.DoesNotExist:
                messages.error(
                    request,
                    _(
                        'This mail either does not exist or cannot be discarded because it was sent already.'
                    ),
                )
                return redirect(self.request.event.orga_urls.outbox)
            if mail.sent:
                messages.error(request, _('This mail had been sent already.'))
            else:
                mail.send(requestor=self.request.user)
                messages.success(request, _('The mail has been sent.'))
            return redirect(self.request.event.orga_urls.outbox)
        return super().dispatch(request, *args, **kwargs)

    @cached_property
    def queryset(self):
        qs = self.request.event.queued_mails.filter(sent__isnull=True)
        if 'pk' in self.kwargs:
            qs = qs.filter(pk=self.kwargs['pk'])
        return qs

    def post(self, request, *args, **kwargs):
        qs = self.queryset
        count = qs.count()
        for mail in qs:
            mail.send(requestor=self.request.user)
        messages.success(
            request, _('{count} mails have been sent.').format(count=count)
        )
        return redirect(self.request.event.orga_urls.outbox)


class OutboxPurge(PermissionRequired, TemplateView):
    permission_required = 'orga.purge_mails'
    template_name = 'orga/mails/confirm.html'

    def get_permission_object(self):
        if 'pk' in self.kwargs:
            return self.request.event.queued_mails.filter(
                sent__isnull=True, pk=self.kwargs.get('pk')
            ).first()
        return self.request.event

    @context
    def question(self):
        return _('Do you really want to purge {count} mails?').format(
            count=self.queryset.count()
        )

    def dispatch(self, request, *args, **kwargs):
        if 'pk' in self.kwargs:
            try:
                mail = self.request.event.queued_mails.get(
                    sent__isnull=True, pk=self.kwargs.get('pk')
                )
            except QueuedMail.DoesNotExist:
                messages.error(
                    request,
                    _(
                        'This mail either does not exist or cannot be discarded because it was sent already.'
                    ),
                )
                return redirect(self.request.event.orga_urls.outbox)
            if mail.sent:
                messages.error(request, _('This mail had been sent already.'))
            else:
                mail.log_action(
                    'pretalx.mail.delete', person=self.request.user, orga=True
                )
                mail.delete()
                messages.success(request, _('The mail has been deleted.'))
            return redirect(request.event.orga_urls.outbox)
        return super().dispatch(request, *args, **kwargs)

    @cached_property
    def queryset(self):
        qs = self.request.event.queued_mails.filter(sent__isnull=True)
        if 'pk' in self.kwargs:
            qs = qs.filter(pk=self.kwargs['pk'])
        return qs

    def post(self, request, *args, **kwargs):
        qs = self.queryset
        count = qs.count()
        qs.delete()
        messages.success(
            request, _('{count} mails have been purged.').format(count=count)
        )
        return redirect(self.request.event.orga_urls.outbox)


class MailDetail(PermissionRequired, ActionFromUrl, CreateOrUpdateView):
    model = QueuedMail
    form_class = MailDetailForm
    template_name = 'orga/mails/outbox_form.html'
    write_permission_required = 'orga.edit_mails'
    permission_required = 'orga.view_mails'

    def get_object(self) -> QueuedMail:
        return self.request.event.queued_mails.filter(pk=self.kwargs.get('pk')).first()

    def get_success_url(self):
        return self.object.event.orga_urls.outbox

    def form_valid(self, form):
        form.instance.event = self.request.event
        if form.instance.sent is not None:
            messages.error(
                self.request,
                _('The email has already been sent, you cannot edit it anymore.'),
            )
            return redirect(self.get_success_url())

        result = super().form_valid(form)
        if form.has_changed():
            action = 'pretalx.mail.' + ('update' if self.object else 'create')
            form.instance.log_action(action, person=self.request.user, orga=True)
        action = form.data.get('form', 'save')
        if action == 'save':
            messages.success(
                self.request,
                _(
                    'The email has been saved. When you send it, the updated text will be used.'
                ),
            )
        elif action == 'send':
            form.instance.send()
            messages.success(self.request, _('The email has been sent.'))
        return result


class MailCopy(PermissionRequired, View):
    permission_required = 'orga.send_mails'

    def get_object(self) -> QueuedMail:
        return get_object_or_404(
            self.request.event.queued_mails, pk=self.kwargs.get('pk')
        )

    def dispatch(self, request, *args, **kwargs):
        mail = self.get_object()
        new_mail = mail.copy_to_draft()
        messages.success(request, _('The mail has been copied, you can edit it now.'))
        return redirect(new_mail.urls.edit)


class ComposeMail(EventPermissionRequired, FormView):
    form_class = WriteMailForm
    template_name = 'orga/mails/send_form.html'
    permission_required = 'orga.send_mails'

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['event'] = self.request.event
        initial = kwargs.get('initial', dict())
        if 'template' in self.request.GET:
            template = MailTemplate.objects.filter(
                pk=self.request.GET.get('template')
            ).first()
            if template:
                initial['subject'] = template.subject
                initial['text'] = template.text
                initial['reply_to'] = template.reply_to
                initial['bcc'] = template.bcc
        if 'submission' in self.request.GET:
            submission = self.request.event.submissions.filter(
                code=self.request.GET.get('submission')
            ).first()
            if submission:
                initial['submissions'] = submission.code
        if 'email' in self.request.GET:
            initial['additional_recipients'] = self.request.GET.get('email')
        kwargs['initial'] = initial
        return kwargs

    def get_success_url(self):
        return self.request.event.orga_urls.compose_mails

    def form_valid(self, form):
        user_set = set()
        submissions = form.cleaned_data.get('submissions')
        if submissions:
            users = User.objects.filter(submissions__in=self.request.event.submissions.filter(
                code__in=submissions
            ))
            user_set.update(users)
        tracks = form.cleaned_data.get('tracks')
        if tracks:
            users = User.objects.filter(submissions__in=self.request.event.submissions.filter(
                track_id__in=tracks
            ))
            user_set.update(users)
        submission_types = form.cleaned_data.get('submission_types')
        if submission_types:
            users = User.objects.filter(submissions__in=self.request.event.submissions.filter(
                submission_type_id__in=submission_types
            ))
            user_set.update(users)

        for recipient in form.cleaned_data.get('recipients'):
            if recipient == 'reviewers':
                users = (
                    User.objects.filter(
                        teams__in=self.request.event.teams.filter(is_reviewer=True)
                    )
                    .distinct()
                )
            elif recipient == 'no_slides':
                users = User.objects.filter(submissions__in=self.request.event.submissions.filter(
                    resources__isnull=True, state='confirmed'))
            else:
                submission_filter = {'state': recipient}  # e.g. "submitted"

                users = User.objects.filter(submissions__in=self.request.event.submissions.filter(
                    **submission_filter
                ))

            user_set.update(users)

        additional_mails = [
            m.strip().lower()
            for m in form.cleaned_data.get('additional_recipients', '').split(',')
            if m.strip()
        ]
        for email in additional_mails:
            user = User.objects.filter(email__iexact=email).first()
            if user:
                user_set.add(user)
            else:
                QueuedMail.objects.create(
                    event=self.request.event,
                    to=email,
                    reply_to=form.cleaned_data.get('reply_to', self.request.event.email),
                    cc=form.cleaned_data.get('cc'),
                    bcc=form.cleaned_data.get('bcc'),
                    subject=form.cleaned_data.get('subject'),
                    text=form.cleaned_data.get('text'),
                )
        for user in user_set:
            mail = QueuedMail.objects.create(
                event=self.request.event,
                reply_to=form.cleaned_data.get('reply_to', self.request.event.email),
                cc=form.cleaned_data.get('cc'),
                bcc=form.cleaned_data.get('bcc'),
                subject=form.cleaned_data.get('subject'),
                text=form.cleaned_data.get('text'),
            )
            mail.to_users.add(user)
        messages.success(
            self.request,
            _(
                '{count} emails have been saved to the outbox – you can make individual changes there or just send them all.'
            ).format(count=len(user_set) + len(additional_mails)),
        )
        return super().form_valid(form)


class TemplateList(EventPermissionRequired, TemplateView):
    template_name = 'orga/mails/template_list.html'
    permission_required = 'orga.view_mail_templates'

    def get_context_data(self, **kwargs):
        result = super().get_context_data(**kwargs)
        accept = self.request.event.accept_template
        ack = self.request.event.ack_template
        reject = self.request.event.reject_template
        update = self.request.event.update_template
        result['accept'] = MailTemplateForm(
            instance=accept, read_only=True, event=self.request.event
        )
        result['ack'] = MailTemplateForm(
            instance=ack, read_only=True, event=self.request.event
        )
        result['reject'] = MailTemplateForm(
            instance=reject, read_only=True, event=self.request.event
        )
        result['update'] = MailTemplateForm(
            instance=update, read_only=True, event=self.request.event
        )
        pks = [
            template.pk if template else None
            for template in [accept, ack, reject, update]
        ]
        result['other'] = [
            MailTemplateForm(
                instance=template, read_only=True, event=self.request.event
            )
            for template in self.request.event.mail_templates.exclude(
                pk__in=[pk for pk in pks if pk]
            )
        ]
        return result


class TemplateDetail(PermissionRequired, ActionFromUrl, CreateOrUpdateView):
    model = MailTemplate
    form_class = MailTemplateForm
    template_name = 'orga/mails/template_form.html'
    permission_required = 'orga.view_mail_templates'
    write_permission_required = 'orga.edit_mail_templates'

    @context
    def placeholders(self):
        template = self.object
        if template and template in template.event.fixed_templates:
            result = get_context_explanation()
            if template == template.event.update_template:
                result.append(
                    {
                        'name': 'notifications',
                        'explanation': _('A list of notifications for this speaker'),
                    }
                )
            return result
        return None

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['event'] = self.request.event
        return kwargs

    def get_object(self) -> MailTemplate:
        return MailTemplate.objects.filter(
            event=self.request.event, pk=self.kwargs.get('pk')
        ).first()

    @cached_property
    def object(self):
        return self.get_object()

    def get_permission_object(self):
        return self.object or self.request.event

    def get_success_url(self):
        return self.request.event.orga_urls.mail_templates

    def form_valid(self, form):
        form.instance.event = self.request.event
        if form.has_changed():
            action = 'pretalx.mail_template.' + ('update' if self.object else 'create')
            form.instance.log_action(action, person=self.request.user, orga=True)
        messages.success(
            self.request,
            'The template has been saved - note that already pending emails that are based on this template will not be changed!',
        )
        return super().form_valid(form)


class TemplateDelete(PermissionRequired, View):
    permission_required = 'orga.edit_mail_templates'

    def get_object(self) -> MailTemplate:
        return get_object_or_404(
            MailTemplate.objects.all(),
            event=self.request.event,
            pk=self.kwargs.get('pk'),
        )

    def dispatch(self, request, *args, **kwargs):
        super().dispatch(request, *args, **kwargs)
        template = self.get_object()
        template.log_action(
            'pretalx.mail_template.delete', person=self.request.user, orga=True
        )
        template.delete()
        messages.success(request, 'The template has been deleted.')
        return redirect(request.event.orga_urls.mail_templates)
