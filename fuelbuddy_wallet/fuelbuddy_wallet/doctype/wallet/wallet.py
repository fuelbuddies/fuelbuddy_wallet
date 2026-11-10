# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""Wallet DocType controller plus the cross-doctype enforcement / balance
upkeep it drives.

Everything wallet-related lives here so the feature is self-contained in the
doctype package; the only piece that must sit outside is the event
registration, in ``hooks.py`` ``doc_events`` (Frappe has no other way to bind a
handler onto Delivery Note / Payment Entry). Those handlers were ported from
DocType-Event Server Scripts; the Frappe event labels map to controller hooks:

    "Before Save"  -> validate
    "After Save"   -> on_update
    "After Submit" -> on_submit

A "Wallet" is the row whose ``payment_type`` is literally "Wallet"; a customer
has at most one.
"""

from types import SimpleNamespace

import frappe
from frappe.model.document import Document
from frappe.utils import add_to_date, cint, flt, getdate, now_datetime, nowdate, time_diff_in_seconds

# Hours a breach stays open before it auto-closes (once the wallet has recovered).
BREACH_WINDOW_HOURS = 12

# Payment Terms Template whose customers get an auto-created wallet. Must match the
# record name exactly -- the Wallet form's customer filter uses the same value.
CASH_ADVANCE_TERMS = "Cash advance"

# Master switch. The shared "Fuelbuddy Settings" single (hosted in fuelbuddy_crm,
# read by all FuelBuddy apps) gates this whole feature via its "Enable Wallet" flag.
# When off (the default), every cross-doctype hook below no-ops: no wallets are
# auto-created on customer onboarding and Delivery Notes are not blocked on wallet
# balance.
FB_SETTINGS_DOCTYPE = "Fuelbuddy Settings"

# Quantity correction (IDEV-3266). fuelbuddy_crm's amend_delivery_note sets this flag on
# the Delivery Note it saves or reissues (doc.flags, so it never reaches another document).
# A correction is approved before it gets here and is never refused for the wallet: the
# on_update / on_cancel / after_delete recompute still books it, even below zero, and crm
# raises the overshoot Issue itself.
QC_AMEND_FLAG = "fb_qc_amend"

LEDGER = "Wallet Ledger Entry"


def _wallet_enabled():
	"""True when the Wallet feature is enabled in Fuelbuddy Settings (default off)."""
	return bool(frappe.db.get_single_value(FB_SETTINGS_DOCTYPE, "enable_wallet"))


class Wallet(Document):
	def validate(self):
		# One wallet per customer: the DN blocker, the recompute and the ledger all key on it.
		if self.payment_type == "Wallet" and (
			self.has_value_changed("customer") or self.has_value_changed("payment_type")
		):
			other = frappe.db.get_value(
				"Wallet",
				{"customer": self.customer, "payment_type": "Wallet", "name": ["!=", self.name or ""]},
				"name",
			)
			if other:
				frappe.throw(f"Customer {self.customer} already has Wallet {other}.")

	def before_save(self):
		# When breach is (re)enabled, stamp date_of_breach = now so the 12h window
		# (close_expired_breaches + the Delivery Note blocker) is measured from the
		# moment it was turned on. Set on the doc itself so it saves in one write and
		# shows on the form immediately (cleaner than a post-save on_update write).
		if self.enable_breach and self.has_value_changed("enable_breach"):
			self.date_of_breach = now_datetime()

	def on_trash(self):
		# the customer's last wallet is gone: its entries stay but nothing counts them
		if not frappe.db.exists("Wallet", {"customer": self.customer, "name": ["!=", self.name]}):
			for entry in _live_entries(customer=self.customer):
				_cancel_entry(entry.name)

	def on_update(self):
		if self.has_value_changed("wallet_start_date") or self.has_value_changed("opening_balance"):
			recompute_from_deliveries(self.name, self.customer)
			if _wallet_enabled():
				sync_wallet_ledger(self.name)


# -- balance helpers ---------------------------------------------------------


def get_customer_wallet(customer):
	"""Return the wallet name for ``customer`` (payment_type == "Wallet"), or None."""
	if not customer:
		return None
	return frappe.db.get_value(
		"Wallet", {"customer": customer, "payment_type": "Wallet"}, "name"
	)


def customer_payments_total(customer, start_date=None):
	"""Wallet opening balance + submitted Payment Entries from the wallet start
	date, company currency: Receive adds, Pay (refund) subtracts."""
	w = frappe.db.get_value(
		"Wallet", {"customer": customer, "payment_type": "Wallet"},
		["wallet_start_date", "opening_balance"], as_dict=True,
	) or {}
	if start_date is None:
		start_date = w.get("wallet_start_date")
	return flt(w.get("opening_balance")) + sum(_pe_amount(pe) or 0.0 for pe in _tracked_payments(customer, start_date))


def _tracked_payments(customer, start_date):
	filters = {"docstatus": 1, "party_type": "Customer", "party": customer}
	if start_date:
		filters["posting_date"] = [">=", start_date]
	return frappe.get_all(
		"Payment Entry",
		filters=filters,
		fields=["name", "posting_date", "company", "payment_type", "base_paid_amount", "base_received_amount"],
	)


def _pe_amount(pe):
	"""Receive adds, Pay (refund) subtracts; anything else is not a wallet movement."""
	if pe.payment_type == "Receive":
		return flt(pe.base_received_amount)
	if pe.payment_type == "Pay":
		return -flt(pe.base_paid_amount)
	return None


def _wallet_start_date(customer):
	"""Date the customer's wallet starts tracking Delivery Notes (None = no wallet)."""
	return frappe.db.get_value(
		"Wallet", {"customer": customer, "payment_type": "Wallet"}, "wallet_start_date"
	)


def _dn_where(customer, start_date, docstatus, exclude=None):
	"""SQL conditions (+ params) selecting the customer's wallet-tracked Delivery
	Notes: posted on or after ``start_date`` (DNs before the wallet started are
	invisible to it), in ``docstatus``, optionally excluding one DN by name."""
	cond = ["dn.customer = %(customer)s", "dn.docstatus in %(docstatus)s"]
	params = {"customer": customer, "docstatus": tuple(docstatus)}
	if start_date:
		cond.append("dn.posting_date >= %(start)s")
		params["start"] = start_date
	if exclude:
		cond.append("dn.name != %(exclude)s")
		params["exclude"] = exclude
	return " and ".join(cond), params


def _billable_rows(customer, rows, as_on=None):
	"""(net value, priced at Force Majeure) per row, AS THE INVOICE WILL BILL IT.

	``rows`` = (so_detail, sales_order, posting_date, item_code, qty, dn_net).
	Mirrors fuelbuddy_crm invoicing in order:
	  1. Force Majeure (IDEV-3129): a delivery whose date falls in a submitted
	     Trigger AND an approved Pricing for this customer bills at the flat
	     agreed rate for the item, deal discount suppressed.
	  2. Everything else is priced off the SALES ORDER LINE (make_sales_invoice
	     bills every litre at the SO line's current price_list_rate / rate, not
	     the price the DN carried on delivery day), then run through the SAME
	     deal-discount formula as auto-invoicing, whose source resolves
	     Opportunity -> Discount doc -> Quotation -> SO, off the catalog list price.
	No SO line -> the DN's own net, unchanged.

	# ponytail: slab qty = the counted DN set per SO, not the invoice cycle;
	# exact for non-slab deals, close for slabs -- widen if finance asks.
	"""
	try:
		from fuelbuddy_crm.auto_invoicing import _apply_quotation_discount
		from fuelbuddy_crm.force_majeure import fm_rate, fm_resolver
	except ImportError:  # crm app absent: wallet falls back to undiscounted DN value
		return [(flt(r[5]), False) for r in rows]
	resolve = fm_resolver(customer)
	out = [None] * len(rows)
	net_qty, delivered_qty = {}, {}  # (sales_order, so_detail) -> qty
	for i, (so_detail, so, on, item_code, qty, dn_net) in enumerate(rows):
		qty = flt(qty)
		pricing = resolve(on) if (resolve and on) else None
		rate = fm_rate(pricing, item_code) if pricing else None
		if rate:
			out[i] = (qty * rate, True)
		elif so_detail and so:
			net_qty[(so, so_detail)] = net_qty.get((so, so_detail), 0.0) + qty
			delivered_qty[(so, so_detail)] = delivered_qty.get((so, so_detail), 0.0) + max(qty, 0.0)
		else:
			out[i] = (flt(dn_net), False)
	per_so = {}
	for (so, so_detail), qty in net_qty.items():
		so_line = frappe.db.get_value("Sales Order Item", so_detail, ["price_list_rate", "rate"], as_dict=True)
		qty = qty if qty > 0 else delivered_qty[(so, so_detail)]  # a fully returned line still prices its rows
		if not so_line or qty <= 0:
			continue
		per_so.setdefault(so, []).append(
			frappe._dict(
				so_detail=so_detail,
				qty=qty,
				price_list_rate=flt(so_line.price_list_rate) or flt(so_line.rate),
				rate=flt(so_line.rate),
			)
		)
	unit = {}  # (sales_order, so_detail) -> billed rate; unchanged when no discount
	for so, lines in per_so.items():
		# SimpleNamespace, not frappe._dict: the formula reads ``si.items`` as an attribute.
		_apply_quotation_discount(
			SimpleNamespace(posting_date=as_on, items=lines),
			frappe.get_cached_doc("Sales Order", so),
		)
		unit.update({(so, line.so_detail): flt(line.rate) for line in lines})
	for i, (so_detail, so, _on, _item, qty, _net) in enumerate(rows):
		if out[i] is None:
			out[i] = (flt(qty) * unit.get((so, so_detail), 0.0), False)
	return out


def _billable_net(customer, rows, as_on=None):
	"""Net value of Delivery Note quantities as the invoice will bill them (see _billable_rows)."""
	return sum(value for value, _fm in _billable_rows(customer, rows, as_on))


def _tracked_dn_value(customer, start_date, docstatus, exclude=None):
	"""Grand-total value (incl. VAT, company currency) of the customer's wallet-tracked
	Delivery Notes as they will be billed: the sum of _dn_values."""
	return sum(dn.value for dn in _dn_values(customer, start_date, docstatus, exclude).values())


def _doc_dn_value(doc):
	"""Same valuation for one in-memory Delivery Note (the one being validated)."""
	rows = [
		(i.so_detail, i.against_sales_order, doc.posting_date, i.item_code, flt(i.qty), flt(i.amount))
		for i in doc.items
	]
	net = flt(doc.net_total)
	vat = flt(doc.base_grand_total) / net if net else 1.0  # VAT + conversion, as above
	return _billable_net(doc.customer, rows, doc.posting_date) * vat


def _dn_values(customer, start_date, docstatus=(0, 1), exclude=None):
	"""{DN: value, fm, posting_date, docstatus, company} for the customer's wallet-tracked
	DNs: _billable_rows per item line, then the DN's own VAT / currency ratio."""
	where, params = _dn_where(customer, start_date, docstatus, exclude)
	rows = frappe.db.sql(
		f"""select dn.name, dni.so_detail, dni.against_sales_order, dn.posting_date, dni.item_code,
			dni.qty, dni.amount, dn.base_grand_total, dn.net_total, dn.docstatus, dn.company
			from `tabDelivery Note Item` dni
			join `tabDelivery Note` dn on dn.name = dni.parent
			where {where}""",
		params,
		as_dict=True,
	)
	values = _billable_rows(
		customer,
		[(r.so_detail, r.against_sales_order, r.posting_date, r.item_code, r.qty, r.amount) for r in rows],
	)
	out = {}
	for r, (value, fm) in zip(rows, values):
		net = flt(r.net_total)
		dn = out.setdefault(
			r.name,
			frappe._dict(
				value=0.0,
				fm=False,
				posting_date=r.posting_date,
				docstatus=r.docstatus,
				company=r.company,
				vat=flt(r.base_grand_total) / net if net else 1.0,
			),
		)
		dn.value += value * dn.vat
		dn.fm = dn.fm or fm
	return out


def customer_delivered_total(customer, start_date=None):
	"""Deal-discounted value of the customer's Delivery Notes (draft + submitted)
	posted on or after the wallet start date."""
	if start_date is None:
		start_date = _wallet_start_date(customer)
	return _tracked_dn_value(customer, start_date, (0, 1))


def recompute_from_deliveries(wallet_name, customer):
	"""Refresh received / delivered / remaining from Payment Entries and Delivery Notes;
	returns the per-DN values so the ledger reuses them."""
	values = _dn_values(customer, _wallet_start_date(customer))
	received = customer_payments_total(customer)
	delivered = sum(dn.value for dn in values.values())
	frappe.db.set_value(
		"Wallet",
		wallet_name,
		{
			"amount_received": received,
			"amount_delivered": delivered,
			"amount_remaining": received - delivered,
		},
	)
	return values


def recompute_received(wallet_name):
	"""Refresh received (and remaining, against the stored delivered) from Payment Entries."""
	customer = frappe.db.get_value("Wallet", wallet_name, "customer")
	received = customer_payments_total(customer)
	delivered = flt(frappe.db.get_value("Wallet", wallet_name, "amount_delivered"))
	frappe.db.set_value(
		"Wallet",
		wallet_name,
		{"amount_received": received, "amount_remaining": received - delivered},
	)


@frappe.whitelist()
def reconcile_wallet(wallet_name, apply=0):
	"""Check the stored received / delivered / remaining against live Payment Entry and
	Delivery Note totals; with ``apply=1`` correct them in place.

	Safety net for bulk submit / cancel flows: every event handler recomputes
	these totals, but a missed or raced event (parallel bulk workers) leaves
	them stale. The recompute is total and idempotent, so correcting is always
	safe. Manual breach fields are never touched.
	"""
	apply = cint(apply)
	if apply:
		# Serialize with live event recomputes on this wallet row.
		frappe.db.get_value("Wallet", wallet_name, "name", for_update=True)
	w = frappe.db.get_value(
		"Wallet",
		wallet_name,
		["customer", "amount_received", "amount_delivered", "amount_remaining"],
		as_dict=True,
	)
	if not w:
		frappe.throw(f"Wallet {wallet_name} not found")
	received = customer_payments_total(w.customer)
	delivered = customer_delivered_total(w.customer)
	expected = {
		"amount_received": received,
		"amount_delivered": delivered,
		"amount_remaining": received - delivered,
	}
	stored = {k: flt(w.get(k)) for k in expected}
	ledger_total = wallet_ledger_total(w.customer)
	in_sync = all(abs(stored[k] - flt(expected[k])) < 0.005 for k in expected) and (
		abs(ledger_total - flt(expected["amount_remaining"])) < 0.005
	)
	corrected = False
	if apply and not in_sync:
		recompute_from_deliveries(wallet_name, w.customer)
		corrected = True
	if apply:
		sync_wallet_ledger(wallet_name)
	return {
		"customer": w.customer,
		"stored": stored,
		"expected": expected,
		"in_sync": in_sync,
		"corrected": corrected,
		"ledger_total": ledger_total,
	}


# -- cross-doctype event handlers (wired in hooks.py doc_events) -------------


def enforce_wallet_balance(doc, method=None):
	"""Delivery Note `before_save` (was "Delivery Note Wallet blocker", Before Save).

	Runs on draft insert and draft re-save only -- never on submit, where Frappe
	would otherwise re-run a `validate` hook and could strand an already-delivered
	DN in draft. Block a Delivery Note that would push the customer's wallet below zero --
	accounting for other open drafts already reserving the balance -- unless an
	active breach allowance covers the shortfall. On a hard block, raise a
	support Issue (in its own transaction so it survives the rollback) and throw.

	A quantity correction (``doc.flags`` QC_AMEND_FLAG) is never blocked: where
	the refusal would come, it returns instead, with no throw and no block Issue.
	Everything before that (breach close / stamp) runs as for any Delivery Note.
	"""
	if not _wallet_enabled():
		return  # feature disabled in Fuelbuddy Settings -> never block on wallet balance
	# for_update: serialize concurrent DN saves for this customer on the wallet row. Without
	# it two deliveries read the same committed/other_drafts, both pass, and the wallet goes
	# negative (TOCTOU). Held to the end of this transaction; a competing save waits, then
	# recomputes with this DN already in the draft set. Same pattern as creditlimit.
	wallet = frappe.db.get_value(
		"Wallet",
		{"customer": doc.customer, "payment_type": "Wallet"},
		[
			"name", "amount_received", "amount_remaining", "enable_breach", "breach_amount",
			"date_of_breach", "wallet_start_date",
		],
		as_dict=True,
		for_update=True,
	)
	if not wallet:
		return
	if wallet.wallet_start_date and getdate(doc.posting_date) < getdate(wallet.wallet_start_date):
		return  # DN predates the wallet: not tracked by it, so never blocked by it

	# Real wallet recovery signal (received - ALL delivered incl. drafts). Used ONLY for the
	# breach-window close below, never for the reservation math.
	remaining = flt(wallet.amount_remaining)

	# Room for THIS delivery = money received, minus value already committed by SUBMITTED DNs,
	# minus value reserved by OTHER open drafts. The current doc is counted exactly once via
	# dn_amount below. We recompute these two sums from live DNs instead of reusing the stored
	# amount_remaining, which already nets ALL drafts (incl. this one on a re-save) and would
	# double-count them — the bug that spuriously blocked a legitimate qty edit.
	# All three are valued as the invoice will bill them (see _billable_net): Force
	# Majeure flat rates and the deal discount, not the raw DN grand_total.
	start = wallet.wallet_start_date
	committed = _tracked_dn_value(doc.customer, start, (1,))
	other_drafts = _tracked_dn_value(doc.customer, start, (0,), exclude=doc.name or "")
	available = flt(wallet.amount_received) - committed - other_drafts
	dn_amount = _doc_dn_value(doc)

	if (available - dn_amount) >= 0:
		return

	shortfall = dn_amount - available

	breach_active = False
	if wallet.enable_breach:
		breach_active = True
		if wallet.date_of_breach:
			hrs = time_diff_in_seconds(now_datetime(), wallet.date_of_breach) / 3600.0
			# close keyed to REAL wallet recovery (remaining), not draft-adjusted available
			if hrs >= BREACH_WINDOW_HOURS and remaining >= 0:
				frappe.db.set_value(
					"Wallet", wallet.name, {"enable_breach": 0, "breach_amount": 0}
				)
				breach_active = False

	allowed = breach_active and (shortfall <= flt(wallet.breach_amount))

	if allowed:
		if not wallet.date_of_breach:
			frappe.db.set_value("Wallet", wallet.name, "date_of_breach", now_datetime())
		return

	if doc.flags.get(QC_AMEND_FLAG):
		return  # approved quantity correction: applied even below zero (see QC_AMEND_FLAG)

	subject = "Wallet balance insufficient for Delivery Note " + str(doc.customer)
	# Reuse an already-open ticket if there is one; otherwise raise a fresh Issue in
	# its own transaction (frappe.enqueue) so it survives the throw-rollback below.
	# The full breakdown lives on the Issue, not in the user-facing block dialog.
	ticket = frappe.db.get_value(
		"Issue", {"subject": subject, "status": ["!=", "Closed"]}, "name"
	)
	if not ticket:
		description = (
			"Customer: " + str(doc.customer) + "<br>"
			"Wallet Start Date: " + str(wallet.wallet_start_date) + "<br>"
			"DN Amount (as billable): " + str(dn_amount) + "<br>"
			"DN Grand Total: " + str(flt(doc.grand_total)) + "<br>"
			"Wallet Received: " + str(flt(wallet.amount_received)) + "<br>"
			"Committed (submitted DNs): " + str(committed) + "<br>"
			"Open Drafts Reserved (others): " + str(other_drafts) + "<br>"
			"Available: " + str(available) + "<br>"
			"Shortfall: " + str(shortfall) + "<br>"
			"Breach Enabled: " + str(wallet.enable_breach) + "<br>"
			"Breach Amount: " + str(flt(wallet.breach_amount))
		)
		frappe.enqueue(
			"frappe.client.insert",
			queue="short",
			doc={
				"doctype": "Issue",
				"subject": subject,
				"priority": "High",
				"issue_type": "Error Log",
				"description": description,
				# Same identity fields erp-functions stamps on its Issues, so ops can trace
				# the blocked punch back to the Hasura invoiced_item / order.
				"customer": doc.customer,
				"custom_invoice_item_id": doc.get("custom_invoiced_item_id"),
				"custom_order_code": doc.get("custom_order_code"),
			},
		)

	# Stop the punch. Frappe can only halt a save by raising, so this stays a throw --
	# but a short one; the detail is on the Issue, not dumped at the user.
	ticket_note = (
		"Support ticket " + str(ticket) + " is open."
		if ticket
		else "A support ticket has been raised."
	)
	frappe.throw(
		"Wallet limit exceeded for " + str(doc.customer) + ". " + ticket_note,
		title="Wallet Limit Exceeded",
	)


def update_wallet_on_delivery_note(doc, method=None):
	"""Delivery Note `on_update` (fires on draft saves and on submit) and
	`on_update_after_submit` (a quantity fixed on a submitted DN).

	Refresh received / delivered / remaining on the customer's wallet by
	recomputing from the live Delivery Notes (docstatus 0/1)."""
	if not _wallet_enabled():
		return
	wallet = get_customer_wallet(doc.customer)
	values = recompute_from_deliveries(wallet, doc.customer) if wallet else None
	post_to_wallet_ledger(doc, values)


def update_wallet_on_delivery_note_cancel(doc, method=None):
	"""Delivery Note `on_cancel` / `after_delete`: the DN leaves the delivered
	set (docstatus 2, or gone), so the same total recompute drops its value and
	frees amount_remaining."""
	if not _wallet_enabled():
		return
	wallet = get_customer_wallet(doc.customer)
	if wallet:
		recompute_from_deliveries(wallet, doc.customer)
	if method == "on_cancel":  # a deleted DN's entry was marked in on_trash
		post_to_wallet_ledger(doc)


def _recompute_received_for_customer(customer):
	"""Shared body of the Payment Entry handlers: submit AND cancel refresh received
	(and remaining) from the customer's Payment Entries."""
	if not _wallet_enabled():
		return
	wallet = get_customer_wallet(customer)
	if wallet:
		recompute_received(wallet)


def update_wallet_on_payment_entry_submit(doc, method=None):
	"""Payment Entry `on_submit` (was "Wallet amount update", After Submit)."""
	if doc.party_type == "Customer":
		_recompute_received_for_customer(doc.party)
		post_to_wallet_ledger(doc)


def update_wallet_on_payment_entry_cancel(doc, method=None):
	"""Payment Entry `on_cancel`: the payment leaves the submitted set, so the
	wallet's received must drop — without this, a cancelled (e.g. bulk-cancelled)
	PE leaves amount_received inflated until an unrelated event recomputes."""
	if doc.party_type == "Customer":
		_recompute_received_for_customer(doc.party)
		post_to_wallet_ledger(doc)


def create_wallet_for_customer(doc, method=None):
	"""Customer `after_insert`: auto-create a Wallet when the new customer is on
	Cash-advance terms. Skips if a wallet already exists for the customer.
	"""
	if not _wallet_enabled():
		return
	if doc.payment_terms != CASH_ADVANCE_TERMS:
		return
	if get_customer_wallet(doc.name):
		return
	frappe.get_doc(
		{
			"doctype": "Wallet",
			"customer": doc.name,
			"payment_terms": doc.payment_terms,
			"payment_type": "Wallet",
		}
	).insert(ignore_permissions=True)


# -- wallet ledger (IDEV-3402): one entry per voucher the wallet counts --------


def _voucher_customer(doc):
	if doc.doctype == "Delivery Note":
		return doc.customer
	return doc.party if doc.party_type == "Customer" else None


def _lock_wallet(customer):
	"""The customer's live Wallet, its row locked: serializes the customer's ledger writes."""
	name = customer and get_customer_wallet(customer)
	if not name:
		return None
	return frappe.db.get_value(
		"Wallet",
		name,
		["name", "customer", "wallet_start_date", "opening_balance", "creation"],
		as_dict=True,
		for_update=True,
	)


def _live_entries(**filters):
	# ponytail: plain read (FOR UPDATE on a missing key gap-locks parallel DN inserts into a deadlock), so a
	# Reconcile racing a voucher's own event can leave a duplicate until the next Reconcile; a unique live key would close it.
	cond = " and ".join(f"`{k}` = %({k})s" for k in filters)
	return frappe.db.sql(
		f"""select name, customer, voucher_type, voucher_no, amount, posting_date, is_submitted,
			force_majeure, force_majeure_change_date from `tabWallet Ledger Entry`
			where is_cancelled = 0 and {cond} order by creation""",
		filters,
		as_dict=True,
	)


def _save_entry(doc):
	doc.flags.from_wallet = doc.flags.ignore_permissions = True
	doc.flags.ignore_links = True  # a cancelled entry's voucher may be deleted
	doc.flags.notify_update = False
	doc.save()


def _cancel_entry(name):
	doc = frappe.get_doc(LEDGER, name, for_update=True)
	doc.is_cancelled = 1
	_save_entry(doc)


def _sync_voucher(live, customer, voucher_type, voucher_no, target, revalue=False):
	"""Insert or update the voucher's one live entry from ``target``; None marks it cancelled."""
	# extra entries (duplicates, a DN moved to another customer) are marked cancelled
	keep = next((e for e in live if e.customer == customer), None) if target else None
	for entry in live:
		if entry is not keep:
			_cancel_entry(entry.name)
	if not target:
		return
	amount, posting_date, is_submitted, company, fm = target
	if not keep:
		doc = frappe.new_doc(LEDGER)
		doc.update(
			{
				"customer": customer,
				"company": company,
				"posting_date": posting_date,
				"voucher_type": voucher_type,
				"voucher_no": voucher_no,
				"amount": amount,
				"is_submitted": is_submitted,
				"force_majeure": cint(fm),
				"force_majeure_change_date": nowdate() if fm else None,
			}
		)
		doc.flags.from_wallet = doc.flags.ignore_permissions = True
		doc.flags.notify_update = False
		doc.submit()
		return
	moved = abs(flt(keep.amount) - flt(amount)) > 1e-6
	before = (getdate(keep.posting_date), cint(keep.is_submitted), cint(keep.force_majeure))
	if not moved and before == (getdate(posting_date), cint(is_submitted), cint(fm)) and (
		keep.force_majeure_change_date and getdate(keep.force_majeure_change_date)
	) == _fm_date(keep, fm, moved, revalue):
		return
	doc = frappe.get_doc(LEDGER, keep.name, for_update=True)
	moved = abs(flt(doc.amount) - flt(amount)) > 1e-6  # the locked row, not the plain read
	doc.update(
		{
			"amount": amount,
			"posting_date": posting_date,
			"is_submitted": is_submitted,
			"force_majeure": cint(fm),
			"force_majeure_change_date": _fm_date(doc, fm, moved, revalue),
		}
	)
	_save_entry(doc)


def _fm_date(entry, fm, moved, revalue):
	"""FM date after this write: set while FM prices the entry, re-dated when FM starts or a Reconcile re-values it, cleared when FM stops."""
	if not fm:
		return None
	if (moved and revalue) or not cint(entry.force_majeure) or not entry.force_majeure_change_date:
		return getdate(nowdate())
	return getdate(entry.force_majeure_change_date)


def post_to_wallet_ledger(doc, values=None):
	"""Make one Delivery Note's or Payment Entry's entry match what the wallet counts for it now."""
	if not _wallet_enabled():
		return
	wallet = _lock_wallet(_voucher_customer(doc))
	target = None
	start = wallet and wallet.wallet_start_date
	if wallet and doc.docstatus < 2 and not (start and getdate(doc.posting_date) < getdate(start)):
		if doc.doctype == "Delivery Note":
			dn = (values if values is not None else _dn_values(wallet.customer, start)).get(doc.name)
			target = (-dn.value if dn else 0.0, doc.posting_date, cint(doc.docstatus == 1), doc.company, bool(dn and dn.fm))
		elif doc.docstatus == 1 and _pe_amount(doc) is not None:
			target = (_pe_amount(doc), doc.posting_date, 1, doc.company, False)
	_sync_voucher(
		_live_entries(voucher_type=doc.doctype, voucher_no=doc.name),
		wallet.customer if wallet else None,
		doc.doctype,
		doc.name,
		target,
	)


def cancel_wallet_entries_on_delete(doc, method=None):
	"""Delivery Note / Payment Entry `on_trash`: the entry stays, marked cancelled."""
	if not _wallet_enabled():
		return
	_lock_wallet(_voucher_customer(doc))
	for entry in _live_entries(voucher_type=doc.doctype, voucher_no=doc.name):
		_cancel_entry(entry.name)


def sync_wallet_ledger(wallet_name):
	"""Re-value every voucher of the wallet and write only the entries that changed;
	entries of vouchers the wallet no longer counts are marked cancelled."""
	w = _lock_wallet(frappe.db.get_value("Wallet", wallet_name, "customer"))
	if not w or w.name != wallet_name:
		return  # a duplicate wallet must not fight the live one over the entries
	start = w.wallet_start_date
	targets = {
		("Delivery Note", name): (-dn.value, dn.posting_date, cint(dn.docstatus == 1), dn.company, dn.fm)
		for name, dn in _dn_values(w.customer, start).items()
	}
	for pe in _tracked_payments(w.customer, start):
		if _pe_amount(pe) is not None:
			targets[("Payment Entry", pe.name)] = (_pe_amount(pe), pe.posting_date, 1, pe.company, False)
	if flt(w.opening_balance):
		targets[("Wallet", w.name)] = (
			flt(w.opening_balance),
			start or getdate(w.creation),
			1,
			frappe.defaults.get_global_default("company"),
			False,
		)
	live = {}
	for entry in _live_entries(customer=w.customer):
		live.setdefault((entry.voucher_type, entry.voucher_no), []).append(entry)
	for key in set(targets) | set(live):
		_sync_voucher(live.get(key, []), w.customer, key[0], key[1], targets.get(key), revalue=True)


def wallet_ledger_total(customer):
	return flt(
		frappe.db.sql(
			"select sum(amount) from `tabWallet Ledger Entry` where customer = %s and is_cancelled = 0",
			customer,
		)[0][0]
	)


# -- scheduled tasks (wired in hooks.py scheduler_events) ---------------------


def close_expired_breaches():
	"""Disable breach and zero out breach_amount for every wallet whose breach has
	been open for at least BREACH_WINDOW_HOURS, measured from date_of_breach.

	Runs on a schedule so a breach expires on its own ~12h after it was enabled,
	independent of any Delivery Note activity. Wallets with breach enabled but no
	date_of_breach are skipped (the <= comparison excludes NULLs); date_of_breach is
	always stamped when breach is enabled (see Wallet.before_save), so that's a no-op
	in practice. Returns the list of closed wallet names.
	"""
	if not _wallet_enabled():
		return []
	cutoff = add_to_date(now_datetime(), hours=-BREACH_WINDOW_HOURS)
	names = frappe.get_all(
		"Wallet",
		filters={"enable_breach": 1, "date_of_breach": ["<=", cutoff]},
		pluck="name",
	)
	for name in names:
		frappe.db.set_value("Wallet", name, {"enable_breach": 0, "breach_amount": 0})
	if names:
		frappe.db.commit()
	return names


def reconcile_all_wallets():
	"""Daily, and the one-off backfill (`bench execute`): Reconcile every wallet, ledger included."""
	if not _wallet_enabled():
		return "Enable Wallet is off in Fuelbuddy Settings: nothing reconciled."
	for name in frappe.get_all("Wallet", filters={"payment_type": "Wallet"}, pluck="name"):
		try:
			reconcile_wallet(name, apply=1)
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title=f"Wallet reconcile failed: {name}")
