##############################################################################
# For copyright and license notices, see __manifest__.py file in module root
# directory
##############################################################################
from odoo import models, fields, api, _
import json
import logging
_logger = logging.getLogger(__name__)


class SaleOrder(models.Model):
    _inherit = "sale.order"

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
        'company_id.l10n_ar_company_requires_vat',)
    def _compute_vat_discriminated(self):
        for rec in self:
            # si tiene checkbook y discrimna en funcion al partner pero no tiene responsabilidad seteada,
            # dejamos comportamiento nativo de odoo de discriminar impuestos
            discriminate_taxes = rec.sale_checkbook_id.discriminate_taxes
            if discriminate_taxes == 'yes':
                vat_discriminated = True
            elif discriminate_taxes == 'no':
                vat_discriminated = False
            else:
                vat_discriminated = rec.company_id.l10n_ar_company_requires_vat and \
                    rec.partner_id.l10n_ar_afip_responsibility_type_id.code in ['1'] or False
            rec.vat_discriminated = vat_discriminated

    @api.onchange('company_id')
    def set_sale_checkbook(self):
        if self.env.user.has_group('l10n_ar_sale.use_sale_checkbook') and \
           self.company_id:
            self.sale_checkbook_id = self.env['sale.checkbook'].search(
                [('company_id', 'in', [self.company_id.id, False])], limit=1)
        else:
            self.sale_checkbook_id = False

    @api.model_create_multi
    def create(self, vals):
        for val in vals:
            if self.env.user.has_group('l10n_ar_sale.use_sale_checkbook') and \
                val.get('name', _('New')) == _('New') and \
                    val.get('sale_checkbook_id'):
                sale_checkbook = self.env['sale.checkbook'].browse(
                    val.get('sale_checkbook_id'))
                val['name'] = sale_checkbook.sequence_id and\
                    sale_checkbook.sequence_id._next() or _('New')
        return super(SaleOrder, self).create(vals)

    # TODO merge this on _compute_tax_totals
    # def _compute_tax_totals_json(self):
    #     # usamos mismo approach que teniamos para facturas en v13, es decir, con esto sabemos si se está solicitando el
    #     # json desde reporte o qweb y en esos casos vemos de incluir impuestos, pero en backend siempre discriminamos
    #     # eventualmente podemos usar mismo approach de facturas en v15 donde ya no se hace asi, si no que cambiamos
    #     # el reporte de facturas usando nuevo metodo _l10n_ar_get_invoice_totals_for_report
    #     report_or_portal_view = 'commit_assetsbundle' in self.env.context or \
    #         not self.env.context.get('params', {}).get('view_type') == 'form'
    #     if not report_or_portal_view:
    #         return super()._compute_tax_totals_json()
    #     for order in self:
    #         # Hacemos esto para disponer de fecha del pedido y cia para calcular
    #         # impuesto con código python (por ej. para ARBA).
    #         # lo correcto seria que esto este en un modulo que dependa de l10n_ar_account_withholding, pero queremos
    #         # evitar ese modulo adicional por ahora
    #         date_order = order.date_order or fields.Date.context_today(order)
    #         order = order.with_context(invoice_date=date_order)
    #         if order.vat_discriminated:
    #             super(SaleOrder, order)._compute_tax_totals_json()
    #         else:
    #             def compute_taxes(order_line):
    #                 price = order_line.price_unit * (1 - (order_line.discount or 0.0) / 100.0)
    #                 order = order_line.order_id
    #                 return order_line.report_tax_id._origin.compute_all(price, order.currency_id, order_line.product_uom_qty, product=order_line.product_id, partner=order.partner_shipping_id)

    #             account_move = self.env['account.move']
    #             for order in self:
    #                 tax_lines_data = account_move._prepare_tax_lines_data_for_totals_from_object(order.order_line, compute_taxes)
    #                 tax_totals = account_move._get_tax_totals(order.partner_id, tax_lines_data, order.amount_total, order.amount_untaxed, order.currency_id)
    #                 order.tax_totals_json = json.dumps(tax_totals)

    def _compute_tax_totals(self):
        """ Mandamos en contexto el invoice_date para calculo de impuesto con partner aliquot
        ver módulo l10n_ar_account_withholding. Además acá reemplazamos el método _compute_tax_totals del módulo sale original de odoo"""
        report_or_portal_view = 'commit_assetsbundle' in self.env.context or \
            not self.env.context.get('params', {}).get('view_type') == 'form'
        if not report_or_portal_view:
            return super()._compute_tax_totals()
        for order in self:
            order = order.with_context(invoice_date=order.date_order)
            order_lines = order.order_line.filtered(lambda x: not x.display_type)
            order.tax_totals = self.env['account.tax']._prepare_tax_totals(
                [x._convert_to_tax_base_line_dict() for x in order_lines],
                order.currency_id or order.company_id.currency_id,
            )

            # Lo que sigue de acá hasta el final del método es medio feo y sirve por si algún presupuesto 
            # cuyo cliente no corresponda discriminar el
            # iva pero tiene alguna percepción o impuesto diferente a iva entonces tenemos que mostrar esa
            # percepción o impuesto diferente y ocultar el iva

            # Esto es para saber todas los impuestos que se están cargando en todas las líneas de la orden de venta
            order_line_taxes = order.order_line.mapped('tax_id.tax_group_id.l10n_ar_vat_afip_code')

            # Acá lo que hacemos es ocultar del presupuesto impreso todos aquellos impuestos que no sean iva cuando
            #  el cliente no discrimina iva
            if not order.vat_discriminated:
                taxes = order.tax_totals['groups_by_subtotal']
                for tax in taxes:
                    contador = 0
                    listado_a_borrar = []
                    for rec1 in taxes[tax]:
                        tax_group_id = rec1['tax_group_id']
                        l10n_ar_vat_afip_code = self.env['account.tax.group'].browse(tax_group_id).l10n_ar_vat_afip_code

                        if l10n_ar_vat_afip_code in order_line_taxes and l10n_ar_vat_afip_code != False:
                            listado_a_borrar.append(contador)
                        contador += 1
                    if len(listado_a_borrar)>0:
                        listado_a_borrar.reverse()
                        for index in listado_a_borrar:
                            taxes[tax].pop(index)


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

    def _l10n_ar_include_vat(self):
        self.ensure_one()
        discriminate_taxes = self.sale_checkbook_id.discriminate_taxes
        if discriminate_taxes == 'yes':
            return False
        elif discriminate_taxes == 'no':
            return True
        else:
            return not (
                self.company_id.l10n_ar_company_requires_vat and
                self.partner_id.l10n_ar_afip_responsibility_type_id.code in ['1'] or False)

    def _create_invoices(self, grouped=False, final=False, date=None):
        """ Por alguna razon cuando voy a crear la factura a traves de una devolucion, no me esta permitiendo crearla
        y validarla porque resulta el campo tipo de documento esta quedando vacio. Este campo se llena y computa
        automaticamente al generar al modificar el diaro de una factura.
        
        Si hacemos la prueba funcional desde la interfaz funciona, si intento importar la factura con el importador de
        Odoo funciona, pero si la voy a crear desde la devolucion inventario no se rellena dicho campo.
        
        Para solventar decimos si tenemos facturas que usan documentos y que no tienen un tipo de documento, intentamos
        computarlo y asignarlo, esto aplica para cuando generamos una factura desde una orden de venta o suscripcion """
        invoices = super()._create_invoices(grouped=grouped, final=final, date=date)
        
        # Intentamos Completar el dato tipo de documento si no seteado 
        to_fix = invoices.filtered(lambda x: x.l10n_latam_use_documents and not x.l10n_latam_document_type_id)
        to_fix._compute_l10n_latam_available_document_types()
        return invoices
