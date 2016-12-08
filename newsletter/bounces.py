import imaplib
import logging
import re

from email.parser import Parser as EmailParser

from django.db.models import Q
from django.utils.encoding import force_text

from .settings import newsletter_settings as settings

logger = logging.getLogger(__name__)

# Regex to catch VERP-encoded address: bounce+bob=example.org@example.net
verp_re = re.compile(r'[^\+]+\+(?P<user>[^=]+)=(?P<domain>[^@]+)@.+')

MAILBOX_FULL = '5.2.2'


def check_bounces():
    """
    Check the settings.BOUNCE_ACCOUNT email account to look for new bounced
    mail.
    Thanks Marc Egli for code inspiration.
    """
    from .models import Bounce, Subscription

    if not settings.BOUNCE_ACCOUNT:
        return

    passed_bounces_folder = 'INBOX.Past bounces'
    try:
        if settings.BOUNCE_ACCOUNT.get('use_ssl'):
            ssl_class = imaplib.IMAP4_SSL
        else:
            ssl_class = imaplib.IMAP4
        conn = ssl_class(settings.BOUNCE_ACCOUNT['host'], int(settings.BOUNCE_ACCOUNT['port']))
        conn.login(settings.BOUNCE_ACCOUNT['username'], settings.BOUNCE_ACCOUNT['password'])
        if conn.select(passed_bounces_folder)[0] != 'OK':
            conn.create(passed_bounces_folder)
        conn.select('INBOX')

        typ, data = conn.search(None, 'ALL')
        for num in data[0].split():
            typ, data = conn.fetch(num, '(RFC822)')
            if not data or not data[0]:
                continue

            # Extract data and create Bounce instance
            msgobj = EmailParser().parsestr(force_text(data[0][1]))
            addr = status = None

            # With VERP, the original destination should be encoded in the To
            ndr_to = msgobj['To']
            res = verp_re.match(ndr_to)
            if res:
                addr = '%s@%s' % (res.group('user'), res.group('domain'))

            for part in msgobj.walk():
                if part.get_content_type() == 'message/delivery-status':
                    for subpart in part.walk():
                        if not addr:
                            if 'Original-Recipient' in subpart:
                                addr = subpart['Original-Recipient'].strip()
                            elif 'Final-Recipient' in subpart:
                                addr = subpart['Final-Recipient'].strip()
                            if addr and 'rfc822;' in addr:
                                addr = addr.replace('rfc822;', '')
                        if 'Status' in subpart:
                            status = subpart['Status']
                        if addr and status:
                            break
                    break
            if not addr or not status:
                continue  # Unable to extract address and status, ignoring...

            for subscr in Subscription.objects.filter(
                    Q(user__email__iexact=addr)|Q(email_field__iexact=addr)):
                hard = status.startswith('5') and status != MAILBOX_FULL
                Bounce.objects.create(
                    subscription=subscr, hard=hard,
                    status_code=status, content=data[0][1],
                )

            # Move handled bounce aside
            if conn.copy(num, passed_bounces_folder)[0] == 'OK':
                conn.store(num, '+FLAGS', r'\Deleted')
        conn.expunge()
        conn.close()
        conn.logout()
    except imaplib.IMAP4.error as e:
        logger.error(e)
