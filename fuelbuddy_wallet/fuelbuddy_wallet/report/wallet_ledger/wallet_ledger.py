# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate


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
	filters.cancelled = cint(filters.cancelled_only)

	entries = frappe.db.sql(
		f"""select name, posting_date, customer, voucher_type, voucher_no, amount, is_submitted,
			is_cancelled, force_majeure_change_date
		from `tabWallet Ledger Entry`
		where posting_date between %(from_date)s and %(to_date)s and is_cancelled = %(cancelled)s and {cond}
		order by posting_date, creation""",
		filters,
		as_dict=True,
	)
	for e in entries:
		e.update(_credit_debit(e.amount), currency=currency)
	credit, debit = sum(e.credit for e in entries), sum(e.debit for e in entries)
	if filters.cancelled:
		# cancelled entries count in no balance: listed with their own total only
		return _columns(), entries + [_total_row(_("Total"), None, credit, debit, currency)]

	opening = flt(
		frappe.db.sql(
			f"""select sum(amount) from `tabWallet Ledger Entry`
			where posting_date < %(from_date)s and is_cancelled = 0 and {cond}""",
			filters,
		)[0][0]
	)
	balance = opening
	for e in entries:
		balance += flt(e.amount)
		e.balance = balance
	closing = opening + credit - debit
	return _columns(), [
		_total_row(_("Opening"), opening, **_credit_debit(opening), currency=currency),
		*entries,
		_total_row(_("Total"), credit - debit, credit, debit, currency),
		_total_row(_("Closing (Opening + Total)"), closing, **_credit_debit(closing), currency=currency),
	]


def _credit_debit(amount):
	amount = flt(amount)
	return {"credit": amount if amount > 0 else 0.0, "debit": -amount if amount < 0 else 0.0}


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
