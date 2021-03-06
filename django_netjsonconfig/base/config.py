from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import ugettext_lazy as _
from model_utils import Choices
from model_utils.fields import StatusField
from sortedm2m.fields import SortedManyToManyField

from .. import settings as app_settings
from .base import BaseConfig


class AbstractConfig(BaseConfig):
    """
    Abstract model implementing the
    NetJSON DeviceConfiguration object
    """
    device = models.OneToOneField('django_netjsonconfig.Device')
    STATUS = Choices('modified', 'running', 'error')
    status = StatusField(help_text=_(
        'modified means the configuration is not applied yet; '
        'running means applied and running; '
        'error means the configuration caused issues and it was rolledback'
    ))
    last_ip = models.GenericIPAddressField(blank=True,
                                           null=True,
                                           help_text=_('indicates the last ip from which the '
                                                       'configuration was downloaded from '
                                                       '(except downloads from this page)'))

    class Meta:
        abstract = True
        verbose_name = _('configuration')
        verbose_name_plural = _('configurations')

    def __str__(self):
        if self._has_device():
            return self.name
        return str(self.pk)

    def clean(self):
        """
        modifies status if key attributes of the configuration
        have changed (queries the database)
        """
        super(AbstractConfig, self).clean()
        if self._state.adding:
            return
        current = self.__class__.objects.get(pk=self.pk)
        for attr in ['backend', 'config']:
            if getattr(self, attr) != getattr(current, attr):
                self.set_status_modified(save=False)
                break

    def _set_status(self, status, save=True):
        self.status = status
        if save:
            self.save()

    def set_status_modified(self, save=True):
        self._set_status('modified', save)

    def set_status_running(self, save=True):
        self._set_status('running', save)

    def set_status_error(self, save=True):
        self._set_status('error', save)

    def _has_device(self):
        return hasattr(self, 'device')

    def get_context(self):
        """
        additional context passed to netjsonconfig
        """
        c = {}
        if self._has_device():
            c.update({
                'id': str(self.device.id),
                'key': self.key,
                'name': self.name,
                'mac_address': self.mac_address
            })
        c.update(app_settings.CONTEXT)
        return c

    @property
    def name(self):
        """
        returns device name
        (kept for backward compatibility with pre 0.6 versions)
        """
        if self._has_device():
            return self.device.name
        return str(self.pk)

    @property
    def mac_address(self):
        """
        returns device mac address
        (kept for backward compatibility with pre 0.6 versions)
        """
        return self.device.mac_address

    @property
    def key(self):
        """
        returns device key
        (kept for backward compatibility with pre 0.6 versions)
        """
        return self.device.key


AbstractConfig._meta.get_field('config').blank = True


class TemplatesThrough(object):
    """
    Improves string representation of m2m relationship objects
    """
    def __str__(self):
        return _('Relationship with {0}').format(self.template.name)


class TemplatesVpnMixin(models.Model):
    """
    Provides a mixin that adds two m2m relationships:
        * Template
        * Vpn
    """
    templates = SortedManyToManyField('django_netjsonconfig.Template',
                                      related_name='config_relations',
                                      verbose_name=_('templates'),
                                      base_class=TemplatesThrough,
                                      blank=True,
                                      help_text=_('configuration templates, applied from '
                                                  'first to last'))
    vpn = models.ManyToManyField('django_netjsonconfig.Vpn',
                                 through='django_netjsonconfig.VpnClient',
                                 related_name='vpn_relations',
                                 blank=True)

    def save(self, *args, **kwargs):
        created = self._state.adding
        super(TemplatesVpnMixin, self).save(*args, **kwargs)
        if created:
            default_templates = self.get_default_templates()
            if default_templates:
                self.templates.add(*default_templates)

    @classmethod
    def get_template_model(cls):
        return cls.templates.rel.model

    def get_default_templates(self):
        """
        retrieves default templates of a Config object
        may be redefined with a custom logic if needed
        """
        qs = self.templates.model.objects.filter(default=True)
        if self.backend:
            qs = qs.filter(backend=self.backend)
        return qs

    @classmethod
    def get_templates_from_pk_set(cls, action, pk_set):
        """
        Retrieves templates from pk_set
        Called in ``clean_templates``, may be reused in third party apps
        """
        if action != 'pre_add':
            return False
        # coming from signal
        if isinstance(pk_set, set):
            template_model = cls.get_template_model()
            templates = template_model.objects.filter(pk__in=list(pk_set))
        # coming from admin ModelForm
        else:
            templates = pk_set
        return templates

    @classmethod
    def clean_templates(cls, action, instance, pk_set, **kwargs):
        """
        validates resulting configuration of config + templates
        raises a ValidationError if invalid
        must be called from forms or APIs
        this method is called from a django signal (m2m_changed)
        see django_netjsonconfig.apps.DjangoNetjsonconfigApp.connect_signals
        """
        templates = cls.get_templates_from_pk_set(action, pk_set)
        if not templates:
            return
        backend = instance.get_backend_instance(template_instances=templates)
        try:
            cls.clean_netjsonconfig_backend(backend)
        except ValidationError as e:
            message = 'There is a conflict with the specified templates. {0}'
            message = message.format(e.message)
            raise ValidationError(message)

    @classmethod
    def templates_changed(cls, action, instance, **kwargs):
        """
        this method is called from a django signal (m2m_changed)
        see django_netjsonconfig.apps.DjangoNetjsonconfigApp.connect_signals
        """
        if action not in ['post_add', 'post_remove', 'post_clear']:
            return
        if instance.status != 'modified':
            instance.set_status_modified()

    @classmethod
    def manage_vpn_clients(cls, action, instance, pk_set, **kwargs):
        """
        automatically manages associated vpn clients if the
        instance is using templates which have type set to "VPN"
        and "auto_cert" set to True.
        This method is called from a django signal (m2m_changed)
        see django_netjsonconfig.apps.DjangoNetjsonconfigApp.connect_signals
        """
        if action not in ['post_add', 'post_remove', 'post_clear']:
            return
        vpn_client_model = cls.vpn.through
        # coming from signal
        if isinstance(pk_set, set):
            template_model = cls.get_template_model()
            templates = template_model.objects.filter(pk__in=list(pk_set))
        # coming from admin ModelForm
        else:
            templates = pk_set
        # when clearing all templates
        if action == 'post_clear':
            for client in instance.vpnclient_set.all():
                client.delete()
            return
        # when adding or removing specific templates
        for template in templates.filter(type='vpn'):
            if action == 'post_add':
                client = vpn_client_model(config=instance,
                                          vpn=template.vpn,
                                          auto_cert=template.auto_cert)
                client.full_clean()
                client.save()
            elif action == 'post_remove':
                for client in instance.vpnclient_set.filter(vpn=template.vpn):
                    client.delete()

    def get_context(self):
        """
        adds VPN client certificates to configuration context
        """
        c = super(TemplatesVpnMixin, self).get_context()
        for vpnclient in self.vpnclient_set.all().select_related('vpn', 'cert'):
            vpn = vpnclient.vpn
            vpn_id = vpn.pk.hex
            context_keys = vpn._get_auto_context_keys()
            ca = vpn.ca
            cert = vpnclient.cert
            # CA
            ca_filename = 'ca-{0}-{1}.pem'.format(ca.pk, ca.common_name)
            ca_path = '{0}/{1}'.format(app_settings.CERT_PATH, ca_filename)
            # update context
            c.update({
                context_keys['ca_path']: ca_path,
                context_keys['ca_contents']: ca.certificate
            })
            # conditional needed for VPN without x509 authentication
            # eg: simple password authentication
            if cert:
                # cert
                cert_filename = 'client-{0}.pem'.format(vpn_id)
                cert_path = '{0}/{1}'.format(app_settings.CERT_PATH, cert_filename)
                # key
                key_filename = 'key-{0}.pem'.format(vpn_id)
                key_path = '{0}/{1}'.format(app_settings.CERT_PATH, key_filename)
                # update context
                c.update({
                    context_keys['cert_path']: cert_path,
                    context_keys['cert_contents']: cert.certificate,
                    context_keys['key_path']: key_path,
                    context_keys['key_contents']: cert.private_key,
                })
        return c

    class Meta:
        abstract = True


# kept for backward compatibility to avoid
# breaking openwisp-controller 0.2.x
# TODO: remove in 0.7.x
sortedm2m__str__ = TemplatesThrough.__str__
