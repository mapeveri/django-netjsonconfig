from django.core.exceptions import ValidationError
from django.db import models
from django.utils.encoding import python_2_unicode_compatible
from django.utils.text import slugify
from django.utils.translation import ugettext_lazy as _

from django_x509.models import Cert

from ..settings import DEFAULT_VPN_BACKENDS
from .base import AbstractConfig


@python_2_unicode_compatible
class BaseVpn(AbstractConfig):
    """
    Abstract VPN model
    """
    name = models.CharField(max_length=64, unique=True)
    host = models.CharField(max_length=64, help_text=_('VPN server hostname or ip address'))
    ca = models.ForeignKey('django_x509.Ca', verbose_name=_('CA'))
    cert = models.ForeignKey('django_x509.Cert',
                             verbose_name=_('x509 Certificate'),
                             blank=True,
                             null=True)
    backend = models.CharField(_('VPN backend'),
                               choices=DEFAULT_VPN_BACKENDS,
                               max_length=128,
                               help_text=_('Select VPN configuration backend'))
    notes = models.TextField(blank=True)

    __vpn__ = True

    class Meta:
        abstract = True

    def clean(self, *args, **kwargs):
        super(BaseVpn, self).clean(*args, **kwargs)
        if self.cert and self.cert.ca.pk is not self.ca.pk:
            msg = _('The selected certificate must match the selected CA.')
            raise ValidationError({'cert': msg})

    def save(self, *args, **kwargs):
        """
        Calls _auto_create_cert() if cert is not set
        """
        if not self.cert:
            self.cert = self._auto_create_cert()
        super(BaseVpn, self).save(*args, **kwargs)

    def _auto_create_cert(self):
        """
        Automatically generates server x509 certificate
        """
        common_name = slugify(self.name)
        server_extensions = [
            {
                "name": "nsCertType",
                "value": "server",
                "critical": False
            }
        ]
        cert = Cert(name=self.name,
                    ca=self.ca,
                    key_length=self.ca.key_length,
                    digest=self.ca.digest,
                    country_code=self.ca.country_code,
                    state=self.ca.state,
                    city=self.ca.city,
                    organization=self.ca.organization,
                    email=self.ca.email,
                    common_name=common_name,
                    extensions=server_extensions)
        cert.save()
        return cert

    def _get_auto_context_keys(self):
        """
        returns a dictionary which indicates the names of
        the configuration variables needed to access:
            * path to CA file
            * CA certificate in PEM format
            * path to cert file
            * cert in PEM format
            * path to key file
            * key in PEM format
        """
        pk = self.pk.hex
        return {
            'ca_path': 'ca_path_{0}'.format(pk),
            'ca_contents': 'ca_contents_{0}'.format(pk),
            'cert_path': 'cert_path_{0}'.format(pk),
            'cert_contents': 'cert_contents_{0}'.format(pk),
            'key_path': 'key_path_{0}'.format(pk),
            'key_contents': 'key_contents_{0}'.format(pk),
        }

    def auto_client(self, auto_cert=True):
        """
        calls backend ``auto_client`` method and returns a configuration
        dictionary that is suitable to be used as a template
        if ``auto_cert`` is ``False`` the resulting configuration
        won't include autogenerated key and certificate details
        """
        config = {}
        backend = self.backend_class
        if hasattr(backend, 'auto_client'):
            context_keys = self._get_auto_context_keys()
            # add curly brackets for netjsonconfig context evaluation
            for key in context_keys.keys():
                context_keys[key] = '{{%s}}' % context_keys[key]
            # do not include cert and key if auto_cert is False
            if not auto_cert:
                for key in ['cert_path', 'cert_contents', 'key_path', 'key_contents']:
                    del context_keys[key]
            conifg_dict_key = self.backend_class.__name__.lower()
            auto = backend.auto_client(host=self.host,
                                       server=self.config[conifg_dict_key][0],
                                       **context_keys)
            config.update(auto)
        return config


class VpnClient(models.Model):
    """
    m2m through model
    """
    config = models.ForeignKey('django_netjsonconfig.Config',
                               on_delete=models.CASCADE)
    vpn = models.ForeignKey('django_netjsonconfig.Vpn',
                            on_delete=models.CASCADE)
    cert = models.OneToOneField('django_x509.Cert',
                                on_delete=models.CASCADE,
                                blank=True,
                                null=True)
    # this flags indicates whether the certificate must be
    # automatically managed, which is going to be almost in all cases
    auto_cert = models.BooleanField(default=False)

    class Meta:
        unique_together = ('config', 'vpn')

    def save(self, *args, **kwargs):
        if self.auto_cert:
            self._auto_create_cert(name=self.config.name,
                                   common_name=self.config.name)
        super(VpnClient, self).save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        delete = self.auto_cert
        super(VpnClient, self).delete(*args, **kwargs)
        if delete:
            self.cert.delete()

    def _auto_create_cert(self, name, common_name):
        """
        Automatically creates and assigns a x509 certificate
        """
        server_extensions = [
            {
                "name": "nsCertType",
                "value": "client",
                "critical": False
            }
        ]
        ca = self.vpn.ca
        cert_model = VpnClient.cert.field.related_model
        cert = cert_model(name=name,
                          ca=ca,
                          key_length=ca.key_length,
                          digest=ca.digest,
                          country_code=ca.country_code,
                          state=ca.state,
                          city=ca.city,
                          organization=ca.organization,
                          email=ca.email,
                          common_name=common_name,
                          extensions=server_extensions)
        cert.save()
        self.cert = cert
        return cert


class Vpn(BaseVpn):
    """
    Concrete VPN model
    """
    class Meta:
        verbose_name = _('VPN Server')
        verbose_name_plural = _('VPN Servers')
