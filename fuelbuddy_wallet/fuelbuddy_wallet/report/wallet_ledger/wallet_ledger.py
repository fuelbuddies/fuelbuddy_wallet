# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import flt, getdate


def execute(filters=None):
	filters = frappe._dict(filters or {})
	if getdate(filters.from_date) > getdate(filters.to_date):
		frappe.throw(_("From Date must be before To Date"))
	currency = frappe.get_cached_value("Company", filters.company, "default_currency")
	cond = ["company = %(company)s"]
	cond += [f"{f} = %({f})s" for f in ("customer", "voucher_type", "voucher_no") if filters.get(f)]
	if filters.submitted_only:
		cond.append("is_submitted = 1")
	cond = " and ".join(cond)

	opening = flt(
		frappe.db.sql(
			f"""select sum(amount) from `tabWallet Ledger Entry`
			where posting_date < %(from_date)s and is_cancelled = 0 and {cond}""",
			filters,
		)[0][0]
	)
	entries = frappe.db.sql(
		f"""select name, posting_date, customer, voucher_type, voucher_no, amount, is_submitted,
			is_cancelled, force_majeure_change_date
		from `tabWallet Ledger Entry`
		where posting_date between %(from_date)s and %(to_date)s and {cond}
			{"" if filters.show_cancelled_entries else "and is_cancelled = 0"}
		order by posting_date, creation""",
		filters,
		as_dict=True,
	)
	data = [_total_row(_("Opening"), opening, max(opening, 0), max(-opening, 0), currency)]
	balance = opening
	credit = debit = 0.0
	for e in entries:
		amount = 0.0 if e.is_cancelled else flt(e.amount)  # listed, never counted
		balance += amount
		credit += max(amount, 0)
		debit += max(-amount, 0)
		e.update(credit=max(flt(e.amount), 0), debit=max(-flt(e.amount), 0), balance=balance, currency=currency)
		data.append(e)
	data.append(_total_row(_("Total"), credit - debit, credit, debit, currency))
	closing = opening + credit - debit
	data.append(_total_row(_("Closing (Opening + Total)"), closing, max(closing, 0), max(-closing, 0), currency))
	return _columns(), data


def _total_row(label, balance, credit, debit, currency):
	return {"voucher_type": label, "credit": credit, "debit": debit, "balance": balance, "currency": currency}


def _columns():
	return [
		{"fieldname": "posting_date", "label": _("Posting Date"), "fieldtype": "Date", "width": 100},
		# Data, not Link to DocType, as in General Ledger: a Link would need read on DocType
		{"fieldname": "voucher_type", "label": _("Voucher Type"), "fieldtype": "Data", "width": 150},
		{"fieldname": "voucher_no", "label": _("Voucher No"), "fieldtype": "Dynamic Link", "options": "voucher_type", "width": 210},
		{"fieldname": "customer", "label": _("Customer"), "fieldtype": "Link", "options": "Customer", "width": 170},
		{"fieldname": "credit", "label": _("Credit"), "fieldtype": "Currency", "options": "currency", "width": 120},
		{"fieldname": "debit", "label": _("Debit"), "fieldtype": "Currency", "options": "currency", "width": 120},
		{"fieldname": "balance", "label": _("Balance"), "fieldtype": "Currency", "options": "currency", "width": 130},
		{"fieldname": "is_submitted", "label": _("Submitted"), "fieldtype": "Check", "width": 90},
		{"fieldname": "is_cancelled", "label": _("Cancelled"), "fieldtype": "Check", "width": 90},
		{"fieldname": "force_majeure_change_date", "label": _("Force Majeure Change Date"), "fieldtype": "Date", "width": 130},
		{"fieldname": "name", "label": _("Wallet Ledger Entry"), "fieldtype": "Link", "options": "Wallet Ledger Entry", "width": 120},
	]
