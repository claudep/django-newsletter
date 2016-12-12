import logging

from django.conf import settings
from django.contrib.sites.models import Site
from django.contrib.sites.managers import CurrentSiteManager
from django.core.mail import EmailMultiAlternatives
from django.core.urlresolvers import reverse
from django.db import models
from django.db.models import Case, Sum, When, permalink
from django.template import Context
from django.template.loader import select_template
from django.utils.encoding import python_2_unicode_compatible
from django.utils.functional import cached_property
from django.utils.translation import ugettext_lazy as _
from django.utils.translation import ugettext
from django.utils.timezone import now

from sorl.thumbnail import ImageField

from .bounces import check_bounces
from .settings import newsletter_settings
from .utils import (
    as_verp, make_activation_code, get_default_sites, ACTIONS
)

logger = logging.getLogger(__name__)

AUTH_USER_MODEL = getattr(settings, 'AUTH_USER_MODEL', 'auth.User')


@python_2_unicode_compatible
class Newsletter(models.Model):
    site = models.ManyToManyField(Site, default=get_default_sites)

    title = models.CharField(
        max_length=200, verbose_name=_('newsletter title')
    )
    slug = models.SlugField(db_index=True, unique=True)

    email = models.EmailField(
        verbose_name=_('e-mail'), help_text=_('Sender e-mail')
    )
    sender = models.CharField(
        max_length=200, verbose_name=_('sender'), help_text=_('Sender name')
    )

    visible = models.BooleanField(
        default=True, verbose_name=_('visible'), db_index=True
    )

    send_html = models.BooleanField(
        default=True, verbose_name=_('send html'),
        help_text=_('Whether or not to send HTML versions of e-mails.')
    )

    objects = models.Manager()

    # Automatically filter the current site
    on_site = CurrentSiteManager()

    def get_templates(self, action):
        """
        Return a subject, text, HTML tuple with e-mail templates for
        a particular action. Returns a tuple with subject, text and e-mail
        template.
        """

        assert action in ACTIONS + ('message', ), 'Unknown action: %s' % action

        # Common substitutions for filenames
        tpl_subst = {
            'action': action,
            'newsletter': self.slug
        }

        # Common root path for all the templates
        tpl_root = 'newsletter/message/'

        subject_template = select_template([
            tpl_root + '%(newsletter)s/%(action)s_subject.txt' % tpl_subst,
            tpl_root + '%(action)s_subject.txt' % tpl_subst,
        ])

        text_template = select_template([
            tpl_root + '%(newsletter)s/%(action)s.txt' % tpl_subst,
            tpl_root + '%(action)s.txt' % tpl_subst,
        ])

        if self.send_html:
            html_template = select_template([
                tpl_root + '%(newsletter)s/%(action)s.html' % tpl_subst,
                tpl_root + '%(action)s.html' % tpl_subst,
            ])
        else:
            # HTML templates are not required
            html_template = None

        return (subject_template, text_template, html_template)

    def __str__(self):
        return self.title

    class Meta:
        verbose_name = _('newsletter')
        verbose_name_plural = _('newsletters')

    @permalink
    def get_absolute_url(self):
        return (
            'newsletter_detail', (),
            {'newsletter_slug': self.slug}
        )

    @permalink
    def subscribe_url(self):
        return (
            'newsletter_subscribe_request', (),
            {'newsletter_slug': self.slug}
        )

    @permalink
    def unsubscribe_url(self):
        return (
            'newsletter_unsubscribe_request', (),
            {'newsletter_slug': self.slug}
        )

    @permalink
    def update_url(self):
        return (
            'newsletter_update_request', (),
            {'newsletter_slug': self.slug}
        )

    @permalink
    def archive_url(self):
        return (
            'newsletter_archive', (),
            {'newsletter_slug': self.slug}
        )

    def get_sender(self):
        return u'%s <%s>' % (self.sender, self.email)

    def get_subscriptions(self):
        logger.debug(u'Looking up subscribers for %s', self)

        return Subscription.objects.filter(newsletter=self).subscribed()

    @classmethod
    def get_default(cls):
        try:
            return cls.objects.all()[0]
        except IndexError:
            return None


class SubscribedQuerySet(models.QuerySet):
    def subscribed(self):
        return self.annotate(
            num_hard_bounces=Sum(
                Case(When(bounce__hard=True, then=1),
                     default=0,
                     output_field=models.IntegerField())
            )
        ).filter(subscribed=True, num_hard_bounces=0)


@python_2_unicode_compatible
class Subscription(models.Model):
    user = models.ForeignKey(
        AUTH_USER_MODEL, blank=True, null=True, verbose_name=_('user'),
        on_delete=models.CASCADE
    )

    name_field = models.CharField(
        db_column='name', max_length=30, blank=True, null=True,
        verbose_name=_('name'), help_text=_('optional')
    )

    def get_name(self):
        if self.user:
            return self.user.get_full_name()
        return self.name_field

    def set_name(self, name):
        if not self.user:
            self.name_field = name
    name = property(get_name, set_name)

    email_field = models.EmailField(
        db_column='email', verbose_name=_('e-mail'), db_index=True,
        blank=True, null=True
    )

    def get_email(self):
        if self.user:
            return self.user.email
        return self.email_field

    def set_email(self, email):
        if not self.user:
            self.email_field = email
    email = property(get_email, set_email)

    @property
    def name_or_email(self):
        return self.name or self.email

    objects = SubscribedQuerySet.as_manager()

    def update(self, action):
        """
        Update subscription according to requested action:
        subscribe/unsubscribe/update/, then save the changes.
        """

        assert action in ('subscribe', 'update', 'unsubscribe')

        # If a new subscription or update, make sure it is subscribed
        # Else, unsubscribe
        if action == 'subscribe' or action == 'update':
            self.subscribed = True
        else:
            self.unsubscribed = True

        logger.debug(
            _(u'Updated subscription %(subscription)s to %(action)s.'),
            {
                'subscription': self,
                'action': action
            }
        )

        # This triggers the subscribe() and/or unsubscribe() methods, taking
        # care of stuff like maintaining the (un)subscribe date.
        self.save()

    def _subscribe(self):
        """
        Internal helper method for managing subscription state
        during subscription.
        """
        logger.debug(u'Subscribing subscription %s.', self)

        self.subscribe_date = now()
        self.subscribed = True
        self.unsubscribed = False

    def _unsubscribe(self):
        """
        Internal helper method for managing subscription state
        during unsubscription.
        """
        logger.debug(u'Unsubscribing subscription %s.', self)

        self.subscribed = False
        self.unsubscribed = True
        self.unsubscribe_date = now()

    def save(self, *args, **kwargs):
        """
        Perform some basic validation and state maintenance of Subscription.

        TODO: Move this code to a more suitable place (i.e. `clean()`) and
        cleanup the code. Refer to comment below and
        https://docs.djangoproject.com/en/dev/ref/models/instances/#django.db.models.Model.clean
        """
        assert self.user or self.email_field, \
            _('Neither an email nor a username is set. This asks for '
              'inconsistency!')
        assert ((self.user and not self.email_field) or
                (self.email_field and not self.user)), \
            _('If user is set, email must be null and vice versa.')

        # This is a lame way to find out if we have changed but using Django
        # API internals is bad practice. This is necessary to discriminate
        # from a state where we have never been subscribed but is mostly for
        # backward compatibility. It might be very useful to make this just
        # one attribute 'subscribe' later. In this case unsubscribed can be
        # replaced by a method property.

        if self.pk:
            assert(Subscription.objects.filter(pk=self.pk).count() == 1)

            subscription = Subscription.objects.get(pk=self.pk)
            old_subscribed = subscription.subscribed
            old_unsubscribed = subscription.unsubscribed

            # If we are subscribed now and we used not to be so, subscribe.
            # If we user to be unsubscribed but are not so anymore, subscribe.
            if ((self.subscribed and not old_subscribed) or
               (old_unsubscribed and not self.unsubscribed)):
                self._subscribe()

                assert not self.unsubscribed
                assert self.subscribed

            # If we are unsubcribed now and we used not to be so, unsubscribe.
            # If we used to be subscribed but are not subscribed anymore,
            # unsubscribe.
            elif ((self.unsubscribed and not old_unsubscribed) or
                  (old_subscribed and not self.subscribed)):
                self._unsubscribe()

                assert not self.subscribed
                assert self.unsubscribed
        else:
            if self.subscribed:
                self._subscribe()
            elif self.unsubscribed:
                self._unsubscribe()

        super(Subscription, self).save(*args, **kwargs)

    ip = models.GenericIPAddressField(_("IP address"), blank=True, null=True)

    newsletter = models.ForeignKey(
        Newsletter, verbose_name=_('newsletter'), on_delete=models.CASCADE
    )

    create_date = models.DateTimeField(editable=False, default=now)

    activation_code = models.CharField(
        verbose_name=_('activation code'), max_length=40,
        default=make_activation_code
    )

    subscribed = models.BooleanField(
        default=False, verbose_name=_('subscribed'), db_index=True
    )
    subscribe_date = models.DateTimeField(
        verbose_name=_("subscribe date"), null=True, blank=True
    )

    # This should be a pseudo-field, I reckon.
    unsubscribed = models.BooleanField(
        default=False, verbose_name=_('unsubscribed'), db_index=True
    )
    unsubscribe_date = models.DateTimeField(
        verbose_name=_("unsubscribe date"), null=True, blank=True
    )

    def __str__(self):
        if self.name:
            return _(u"%(name)s <%(email)s> to %(newsletter)s") % {
                'name': self.name,
                'email': self.email,
                'newsletter': self.newsletter
            }

        else:
            return _(u"%(email)s to %(newsletter)s") % {
                'email': self.email,
                'newsletter': self.newsletter
            }

    class Meta:
        verbose_name = _('subscription')
        verbose_name_plural = _('subscriptions')
        unique_together = ('user', 'email_field', 'newsletter')

    def get_recipient(self):
        if self.name:
            return u'%s <%s>' % (self.name, self.email)

        return u'%s' % (self.email)

    def send_activation_email(self, action):
        assert action in ACTIONS, 'Unknown action: %s' % action

        (subject_template, text_template, html_template) = \
            self.newsletter.get_templates(action)

        variable_dict = {
            'subscription': self,
            'site': Site.objects.get_current(),
            'newsletter': self.newsletter,
            'date': self.subscribe_date,
            'STATIC_URL': settings.STATIC_URL,
            'MEDIA_URL': settings.MEDIA_URL
        }

        unescaped_context = Context(variable_dict, autoescape=False)

        subject = subject_template.render(unescaped_context).strip()
        text = text_template.render(unescaped_context)

        message = EmailMultiAlternatives(
            subject, text,
            from_email=self.newsletter.get_sender(),
            to=[self.email]
        )

        if html_template:
            escaped_context = Context(variable_dict)

            message.attach_alternative(
                html_template.render(escaped_context), "text/html"
            )

        message.send()

        logger.debug(
            u'Activation email sent for action "%(action)s" to %(subscriber)s '
            u'with activation code "%(action_code)s".', {
                'action_code': self.activation_code,
                'action': action,
                'subscriber': self
            }
        )

    @permalink
    def subscribe_activate_url(self):
        return ('newsletter_update_activate', (), {
            'newsletter_slug': self.newsletter.slug,
            'email': self.email,
            'action': 'subscribe',
            'activation_code': self.activation_code
        })

    @permalink
    def unsubscribe_activate_url(self):
        return ('newsletter_update_activate', (), {
            'newsletter_slug': self.newsletter.slug,
            'email': self.email,
            'action': 'unsubscribe',
            'activation_code': self.activation_code
        })

    @permalink
    def update_activate_url(self):
        return ('newsletter_update_activate', (), {
            'newsletter_slug': self.newsletter.slug,
            'email': self.email,
            'action': 'update',
            'activation_code': self.activation_code
        })


# From https://tools.ietf.org/html/rfc3463
SMTP_ERROR_CODES = {
    '5.0.0': "Other undefined Status",
    '5.1.0': "Other address status",
    '5.1.1': "Bad destination mailbox address",
    '5.1.2': "Bad destination system address",
    '5.1.3': "Bad destination mailbox address syntax",
    '5.1.4': "Destination mailbox address ambiguous",
    '5.1.6': "Destination mailbox has moved, No forwarding address",
    '5.1.7': "Bad sender's mailbox address syntax",
    '5.1.8': "Bad sender's system address",
    '5.2.0': "Other or undefined mailbox status",
    '5.2.1': "Mailbox disabled, not accepting messages",
    '5.2.2': "Mailbox full",
    '5.2.3': "Message length exceeds administrative limit",
    '5.2.4': "Mailing list expansion problem",
    '5.3.0': "Other or undefined mail system status",
    '5.3.1': "Mail system full",
    '5.3.2': "System not accepting network messages",
    '5.3.3': "System not capable of selected features",
    '5.3.4': "Message too big for system",
    '5.3.5': "System incorrectly configured",
    '5.4.0': "Other or undefined network or routing status",
    '5.4.1': "No answer from host",
    '5.4.2': "Bad connection",
    '5.4.3': "Directory server failure",
    '5.4.4': "Unable to route",
    '5.4.5': "Mail system congestion",
    '5.4.6': "Routing loop detected",
    '5.4.7': "Delivery time expired",
    '5.5.0': "Other or undefined protocol status",
    '5.5.1': "Invalid command",
    '5.5.2': "Syntax error",
    '5.5.3': "Too many recipients",
    '5.5.4': "Invalid command arguments",
    '5.5.5': "Wrong protocol version",
    '5.6.0': "Other or undefined media error",
    '5.6.1': "Media not supported",
    '5.6.2': "Conversion required and prohibited",
    '5.6.3': "Conversion required but not supported",
    '5.6.4': "Conversion with loss performed",
    '5.6.5': "Conversion Failed",
    '5.7.0': "Other or undefined security status",
    '5.7.1': "Delivery not authorized, message refused",
    '5.7.2': "Mailing list expansion prohibited",
    '5.7.3': "Security conversion required but not possible",
    '5.7.4': "Security features not supported",
    '5.7.5': "Cryptographic failure",
    '5.7.6': "Cryptographic algorithm not supported",
    '5.7.7': "Message integrity failure",
    # MS-specific ?
    '5.7.606': "Access denied, banned sending IP",
}


@python_2_unicode_compatible
class Bounce(models.Model):
    """
    A bounce message received after sending a message, due to some transient or
    permanent error.
    """
    subscription = models.ForeignKey(Subscription, verbose_name=_('subscription'),
        on_delete=models.CASCADE)
    date_create = models.DateTimeField(
        verbose_name=_('created'), auto_now_add=True, editable=False
    )
    hard = models.BooleanField(default=False, verbose_name=_('hard bounce'))
    status_code = models.CharField(max_length=20, verbose_name=_('status code'))
    content = models.TextField(verbose_name=_('content'))

    class Meta:
        verbose_name = _('bounce')
        verbose_name_plural = _('bounces')

    def __str__(self):
        return "%(type)s bounce for %(email)s (%(code)s)" % {
            'type': 'Hard' if self.hard else 'Soft',
            'email': self.subscription.email,
            'code': self.status_code,
        }

    @property
    def status_string(self):
        try:
            return SMTP_ERROR_CODES[self.status_code]
        except KeyError:
            return 'Unknown error code'


@python_2_unicode_compatible
class Article(models.Model):
    """
    An Article within a Message which will be send through a Submission.
    """

    sortorder = models.PositiveIntegerField(
        help_text=_('Sort order determines the order in which articles are '
                    'concatenated in a post.'),
        verbose_name=_('sort order'), blank=True
    )

    title = models.CharField(max_length=200, verbose_name=_('title'))
    text = models.TextField(verbose_name=_('text'))

    url = models.URLField(
        verbose_name=_('link'), blank=True, null=True
    )

    # Make this a foreign key for added elegance
    image = ImageField(
        upload_to='newsletter/images/%Y/%m/%d', blank=True, null=True,
        verbose_name=_('image')
    )

    # Message this article is associated with
    # TODO: Refactor post to message (post is legacy notation).
    post = models.ForeignKey(
        'Message', verbose_name=_('message'), related_name='articles',
        on_delete=models.CASCADE
    )

    class Meta:
        ordering = ('sortorder',)
        verbose_name = _('article')
        verbose_name_plural = _('articles')
        unique_together = ('post', 'sortorder')

    def __str__(self):
        return self.title

    def save(self):
        if self.sortorder is None:
            # If saving a new object get the next available Article ordering
            # as to assure uniqueness.
            self.sortorder = self.post.get_next_article_sortorder()

        super(Article, self).save()


@python_2_unicode_compatible
class Message(models.Model):
    """ Message as sent through a Submission. """

    title = models.CharField(max_length=200, verbose_name=_('title'))
    slug = models.SlugField(verbose_name=_('slug'))

    newsletter = models.ForeignKey(
        Newsletter, verbose_name=_('newsletter'), on_delete=models.CASCADE
    )

    date_create = models.DateTimeField(
        verbose_name=_('created'), auto_now_add=True, editable=False
    )
    date_modify = models.DateTimeField(
        verbose_name=_('modified'), auto_now=True, editable=False
    )

    class Meta:
        verbose_name = _('message')
        verbose_name_plural = _('messages')
        unique_together = ('slug', 'newsletter')

    def __str__(self):
        try:
            return _(u"%(title)s in %(newsletter)s") % {
                'title': self.title,
                'newsletter': self.newsletter
            }
        except Newsletter.DoesNotExist:
            logger.warning('No newsletter has been set for this message yet.')
            return self.title

    def save(self, **kwargs):
        if self.pk is None:
            self.newsletter = Newsletter.get_default()
        super(Message, self).save(**kwargs)

    def get_next_article_sortorder(self):
        """ Get next available sortorder for Article. """

        next_order = self.articles.aggregate(
            models.Max('sortorder')
        )['sortorder__max']

        if next_order:
            return next_order + 10
        else:
            return 10

    @cached_property
    def _templates(self):
        """Return a (subject_template, text_template, html_template) tuple."""
        return self.newsletter.get_templates('message')

    @property
    def subject_template(self):
        return self._templates[0]

    @property
    def text_template(self):
        return self._templates[1]

    @property
    def html_template(self):
        return self._templates[2]

    @classmethod
    def get_default(cls):
        try:
            return cls.objects.order_by('-date_create').all()[0]
        except IndexError:
            return None


@python_2_unicode_compatible
class Submission(models.Model):
    """
    Submission represents a particular Message as it is being submitted
    to a list of Subscribers. This is where actual queueing and submission
    happen.
    """
    class Meta:
        verbose_name = _('submission')
        verbose_name_plural = _('submissions')

    def __str__(self):
        return _(u"%(newsletter)s on %(publish_date)s") % {
            'newsletter': self.message,
            'publish_date': self.publish_date
        }

    @cached_property
    def extra_headers(self):
        headers = {
            'List-Unsubscribe': 'http://%s%s' % (
                Site.objects.get_current().domain,
                reverse('newsletter_unsubscribe_request',
                        args=[self.message.newsletter.slug])
            ),
        }
        if self.bounce_address:
            # `From:` header will be different from `MAIL FROM`
            headers['From'] = self.newsletter.get_sender()
        return headers

    @cached_property
    def bounce_address(self):
        if newsletter_settings.BOUNCE_ACCOUNT and 'email' in newsletter_settings.BOUNCE_ACCOUNT:
            return newsletter_settings.BOUNCE_ACCOUNT['email']
        return None

    def submit(self):
        subscriptions = self.subscriptions.subscribed()

        logger.info(
            ugettext(u"Submitting %(submission)s to %(count)d people"),
            {'submission': self, 'count': subscriptions.count()}
        )

        assert self.publish_date < now(), \
            'Something smells fishy; submission time in future.'

        self.sending = True
        self.save()

        try:
            for subscription in subscriptions:
                self.send_message(subscription)
            self.sent = True

        finally:
            self.sending = False
            self.save()

    def send_message(self, subscription):
        variable_dict = {
            'subscription': subscription,
            'site': Site.objects.get_current(),
            'submission': self,
            'message': self.message,
            'newsletter': self.newsletter,
            'date': self.publish_date,
            'STATIC_URL': settings.STATIC_URL,
            'MEDIA_URL': settings.MEDIA_URL
        }

        unescaped_context = Context(variable_dict, autoescape=False)

        subject = self.message.subject_template.render(
            unescaped_context).strip()
        text = self.message.text_template.render(unescaped_context)

        message = EmailMultiAlternatives(
            subject, text,
            from_email=(as_verp(self.bounce_address, subscription.email)
                        or self.newsletter.get_sender()),
            to=[subscription.get_recipient()],
            headers=self.extra_headers,
        )

        if self.message.html_template:
            escaped_context = Context(variable_dict)

            message.attach_alternative(
                self.message.html_template.render(escaped_context),
                "text/html"
            )

        try:
            logger.debug(
                ugettext(u'Submitting message to: %s.'),
                subscription
            )

            message.send()

        except Exception as e:
            # TODO: Test coverage for this branch.
            logger.error(
                ugettext(u'Message %(subscription)s failed '
                         u'with error: %(error)s'),
                {'subscription': subscription,
                 'error': e}
            )

    @classmethod
    def submit_queue(cls):
        todo = cls.objects.filter(
            prepared=True, sent=False, sending=False,
            publish_date__lt=now()
        )

        for submission in todo:
            submission.submit()

        check_bounces()

    @classmethod
    def from_message(cls, message):
        logger.debug(ugettext('Submission of message %s'), message)
        submission = cls()
        submission.message = message
        submission.newsletter = message.newsletter
        submission.save()
        submission.subscriptions = message.newsletter.get_subscriptions()
        return submission

    def save(self):
        """ Set the newsletter from associated message upon saving. """
        assert self.message.newsletter

        self.newsletter = self.message.newsletter

        return super(Submission, self).save()

    @permalink
    def get_absolute_url(self):
        assert self.newsletter.slug
        assert self.message.slug

        return (
            'newsletter_archive_detail', (), {
                'newsletter_slug': self.newsletter.slug,
                'year': self.publish_date.year,
                'month': self.publish_date.month,
                'day': self.publish_date.day,
                'slug': self.message.slug
            }
        )

    newsletter = models.ForeignKey(
        Newsletter, verbose_name=_('newsletter'), editable=False,
        on_delete=models.CASCADE
    )
    message = models.ForeignKey(
        Message, verbose_name=_('message'), editable=True, null=False,
        on_delete=models.CASCADE
    )

    subscriptions = models.ManyToManyField(
        'Subscription',
        help_text=_('If you select none, the system will automatically find '
                    'the subscribers for you.'),
        blank=True, db_index=True, verbose_name=_('recipients'),
        limit_choices_to={'subscribed': True}
    )

    publish_date = models.DateTimeField(
        verbose_name=_('publication date'), blank=True, null=True,
        default=now, db_index=True
    )
    publish = models.BooleanField(
        default=True, verbose_name=_('publish'),
        help_text=_('Publish in archive.'), db_index=True
    )

    prepared = models.BooleanField(
        default=False, verbose_name=_('prepared'),
        db_index=True, editable=False
    )
    sent = models.BooleanField(
        default=False, verbose_name=_('sent'),
        db_index=True, editable=False
    )
    sending = models.BooleanField(
        default=False, verbose_name=_('sending'),
        db_index=True, editable=False
    )
