##############################################################################
# For copyright and license notices, see __manifest__.py file in module root
# directory
##############################################################################
from odoo import models, fields, api, _
from functools import partial
from odoo.tools.misc import formatLang
# from odoo.exceptions import UserError
import logging
import json
_logger = logging.getLogger(__name__)


class SaleOrder(models.Model):
    _inherit = "sale.order"

    report_amount_untaxed = fields.Monetary(
        compute='_compute_report_report_amount_untaxed'
    )
    vat_discriminated = fields.Boolean(
        compute='_compute_vat_discriminated',
    )
    sale_checkbook_id = fields.Many2one(
        'sale.checkbook',
        readonly=True,
        states={'draft': [('readonly', False)], 'sent': [('readonly', False)]},
    )

    @api.depends(
        'partner_id.l10n_ar_afip_responsibility_type_id',
        'company_id.partner_id.l10n_ar_afip_responsibility_type_id',)
    def _compute_vat_discriminated(self):
        use_sale_checkbook = self.env.user.has_group(
            'l10n_ar_sale.use_sale_checkbook')
        for rec in self:
            l10n_ar_afip_responsibility_type = rec.partner_id.commercial_partner_id.l10n_ar_afip_responsibility_type_id
            # si tiene checkbook y discrimna en funcion al partner pero
            # no tiene responsabilidad seteada, dejamos comportamiento nativo
            # de odoo de discriminar impuestos
            if use_sale_checkbook and rec.sale_checkbook_id:
                discriminate_taxes = rec.sale_checkbook_id.discriminate_taxes
                if discriminate_taxes == 'yes':
                    vat_discriminated = True
                elif discriminate_taxes == 'no':
                    vat_discriminated = False
                else:
                    vat_discriminated = rec.company_id.l10n_ar_company_requires_vat and \
                        l10n_ar_afip_responsibility_type.code in ['1'] or False
                rec.vat_discriminated = vat_discriminated
                continue

            # dejamos esto por compatibilidad hacia atras sin sale.checkbook
            vat_discriminated = True
            company_vat_type = rec.company_id.sale_allow_vat_no_discrimination
            if company_vat_type and company_vat_type != 'discriminate':
                if l10n_ar_afip_responsibility_type:
                    # si tiene responsabilidad evaluamos segun responsabilidad
                    vat_discriminated = rec.company_id.l10n_ar_company_requires_vat and \
                        l10n_ar_afip_responsibility_type.code in ['1'] or False
                elif company_vat_type == 'no_discriminate_default':
                    # si no tiene responsabilidad y no se discriinar por defecto, no discriminamos
                    vat_discriminated = False
                # si no tenia responsabilidad y no era no_discriminate_default, entonces es discriminate_default
                # dejamos el vat_discriminated = True
            rec.vat_discriminated = vat_discriminated

    @api.depends('amount_untaxed', 'vat_discriminated')
    def _compute_report_report_amount_untaxed(self):
        """
        Similar a account_document intoive pero por ahora incluimos o no todos
        los impuestos (TODO mejorar y solo incluir impuestos IVA)
        """
        for order in self:
            taxes_included = not order.vat_discriminated
            if not taxes_included:
                report_amount_untaxed = order.amount_untaxed
            else:
                report_amount_untaxed = order.amount_total - sum(
                    x[1] for x in order.tax_totals_json)
            order.report_amount_untaxed = report_amount_untaxed

    @api.onchange('company_id')
    def set_sale_checkbook(self):
        if self.env.user.has_group('l10n_ar_sale.use_sale_checkbook') and \
           self.company_id:
            self.sale_checkbook_id = self.env['sale.checkbook'].search(
                [('company_id', 'in', [self.company_id.id, False])], limit=1)
        else:
            self.sale_checkbook_id = False

    @api.model
    def create(self, vals):
        if self.env.user.has_group('l10n_ar_sale.use_sale_checkbook') and \
            vals.get('name', _('New')) == _('New') and \
                vals.get('sale_checkbook_id'):
            sale_checkbook = self.env['sale.checkbook'].browse(
                vals.get('sale_checkbook_id'))
            vals['name'] = sale_checkbook.sequence_id and\
                sale_checkbook.sequence_id._next() or _('New')
        return super(SaleOrder, self).create(vals)


    def write(self, vals):
        """A sale checkbook could have a different order sequence, so we could
        need to change it accordingly"""
        if self.env.user.has_group('l10n_ar_sale.use_sale_checkbook') and vals.get('sale_checkbook_id'):
            sale_checkbook = self.env['sale.checkbook'].browse(vals['sale_checkbook_id'])
            if sale_checkbook.sequence_id:
                for record in self:
                    if (
                        record.state in {"draft", "sent"}
                        and record.type_id.sequence_id != sale_checkbook.sequence_id
                    ):
                        new_vals = vals.copy()
                        new_vals["name"] = sale_checkbook.sequence_id._next() or _('New')
                        super(SaleOrder, record).write(new_vals)
                    else:
                        super(SaleOrder, record).write(vals)
                return True
        return super().write(vals)


    def _amount_by_group(self):
        for order in self:
            # Hacemos esto para disponer de fecha del pedido y cia para calcular
            # impuesto con código python (por ej. para ARBA).
            # lo correcto seria que esto este en un modulo que dependa de l10n_ar_account_withholding, pero queremos
            # evitar ese modulo adicional por ahora
            date_order = order.date_order or fields.Date.context_today(order)
            order = order.with_context(invoice_date=date_order)
            if order.vat_discriminated:
                super(SaleOrder, order)._amount_by_group()
            else:
                currency = order.currency_id or order.company_id.currency_id
                fmt = partial(formatLang, self.with_context(lang=order.partner_id.lang).env, currency_obj=currency)
                res = {}
                for line in order.order_line:
                    price_reduce = line.price_unit * (1.0 - line.discount / 100.0)
                    taxes = line.report_tax_id.compute_all(
                        price_reduce, quantity=line.product_uom_qty, product=line.product_id,
                        partner=order.partner_shipping_id)['taxes']
                    for tax in line.report_tax_id:
                        group = tax.tax_group_id
                        res.setdefault(group, {'amount': 0.0, 'base': 0.0})
                        for t in taxes:
                            if t['id'] == tax.id or t['id'] in tax.children_tax_ids.ids:
                                res[group]['amount'] += t['amount']
                                res[group]['base'] += t['base']
                res = sorted(res.items(), key=lambda l: l[0].sequence)
                order.amount_by_group = [(
                    l[0].name, l[1]['amount'], l[1]['base'],
                    fmt(l[1]['amount']), fmt(l[1]['base']),
                    len(res),
                ) for l in res]

    def _get_name_sale_report(self, report_xml_id):
        """ Method similar to the '_get_name_invoice_report' of l10n_latam_invoice_document
        Basically it allows different localizations to define it's own report
        This method should actually go in a sale_ux module that later can be extended by different localizations
        Another option would be to use report_substitute module and setup a subsitution with a domain
        """
        self.ensure_one()
        if self.company_id.country_id.code == 'AR':
            return 'l10n_ar_sale.report_saleorder_document'
        return report_xml_id

    # @api.depends('order_line.tax_id', 'order_line.price_unit', 'amount_total', 'amount_untaxed')
    def _compute_tax_totals_json(self):
        super()._compute_tax_totals_json()
        for order in self:
            order.tax_totals_json = json.loads(order.tax_totals_json).update({'amount_untaxed': order.report_amount_untaxed})
            # Hacemos esto para disponer de fecha del pedido y cia para calcular
            # impuesto con código python (por ej. para ARBA).
            # lo correcto seria que esto este en un modulo que dependa de l10n_ar_account_withholding, pero queremos
            # evitar ese modulo adicional por ahora
            date_order = order.date_order or fields.Date.context_today(order)
            order = order.with_context(invoice_date=date_order)
            if not order.vat_discriminated:
                currency = order.currency_id or order.company_id.currency_id
                fmt = partial(formatLang, self.with_context(lang=order.partner_id.lang).env, currency_obj=currency)
                res = {}
                for line in order.order_line:
                    price_reduce = line.price_unit * (1.0 - line.discount / 100.0)
                    taxes = line.report_tax_id.compute_all(
                        price_reduce, quantity=line.product_uom_qty, product=line.product_id,
                        partner=order.partner_shipping_id)['taxes']
                    for tax in line.report_tax_id:
                        group = tax.tax_group_id
                        res.setdefault(group, {'amount': 0.0, 'base': 0.0})
                        for t in taxes:
                            if t['id'] == tax.id or t['id'] in tax.children_tax_ids.ids:
                                res[group]['amount'] += t['amount']
                                res[group]['base'] += t['base']
                res = sorted(res.items(), key=lambda l: l[0].sequence)
                tax_totals_json = [(
                    l[0].name, l[1]['amount'], l[1]['base'],
                    fmt(l[1]['amount']), fmt(l[1]['base']),
                    len(res),
                ) for l in res]
                print(tax_totals_json)
                print(order.tax_totals_json)
                print("*********--------******----------")
