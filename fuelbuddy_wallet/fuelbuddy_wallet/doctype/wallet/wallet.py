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

import frappe
from frappe.model.document import Document
from frappe.utils import add_to_date, cint, flt, getdate, now_datetime, time_diff_in_seconds

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


def _wallet_enabled():
	"""True when the Wallet feature is enabled in Fuelbuddy Settings (default off)."""
	return bool(frappe.db.get_single_value(FB_SETTINGS_DOCTYPE, "enable_wallet"))


class Wallet(Document):
	def before_save(self):
		# When breach is (re)enabled, stamp date_of_breach = now so the 12h window
		# (close_expired_breaches + the Delivery Note blocker) is measured from the
		# moment it was turned on. Set on the doc itself so it saves in one write and
		# shows on the form immediately (cleaner than a post-save on_update write).
		if self.enable_breach and self.has_value_changed("enable_breach"):
			self.date_of_breach = now_datetime()


# -- balance helpers ---------------------------------------------------------


def get_customer_wallet(customer):
	"""Return the wallet name for ``customer`` (payment_type == "Wallet"), or None."""
	if not customer:
		return None
	return frappe.db.get_value(
		"Wallet", {"customer": customer, "payment_type": "Wallet"}, "name"
	)


def customer_gl_balance(customer):
	"""Net GL balance (credit - debit) over non-cancelled GL Entries for the customer."""
	rows = frappe.db.get_all(
		"GL Entry",
		filters={"is_cancelled": 0, "party_type": "Customer", "party": customer},
		fields=["credit_in_account_currency", "debit_in_account_currency"],
	)
	ledger = 0.0
	for r in rows:
		ledger += (r.credit_in_account_currency or 0) - (r.debit_in_account_currency or 0)
	return ledger


def _wallet_start_date(customer):
	"""Date the customer's wallet starts tracking Delivery Notes (None = no wallet)."""
	return frappe.db.get_value(
		"Wallet", {"customer": customer, "payment_type": "Wallet"}, "wallet_start_date"
	)


def _dn_filters(customer, start_date, **extra):
	"""Delivery Note filters for the wallet: the customer's DNs posted on or after
	``start_date``. DNs before the wallet started are invisible to it."""
	filters = {"customer": customer, **extra}
	if start_date:
		filters["posting_date"] = [">=", start_date]
	return filters


def customer_delivered_total(customer, start_date=None):
	"""Sum of Delivery Note grand_total (draft + submitted) for the customer,
	counting only DNs posted on or after the wallet start date."""
	if start_date is None:
		start_date = _wallet_start_date(customer)
	rows = frappe.db.get_all(
		"Delivery Note",
		filters=_dn_filters(customer, start_date, docstatus=["in", [0, 1]]),
		fields=["grand_total"],
	)
	return sum((d.grand_total or 0) for d in rows)


def recompute_from_deliveries(wallet_name, customer):
	"""Refresh received / delivered / remaining from GL and Delivery Notes."""
	ledger = customer_gl_balance(customer)
	delivered = customer_delivered_total(customer)
	frappe.db.set_value(
		"Wallet",
		wallet_name,
		{
			"amount_received": ledger,
			"amount_delivered": delivered,
			"amount_remaining": ledger - delivered,
		},
	)


def recompute_received(wallet_name):
	"""Refresh received (and remaining, against the stored delivered) from GL."""
	customer = frappe.db.get_value("Wallet", wallet_name, "customer")
	ledger = customer_gl_balance(customer)
	delivered = flt(frappe.db.get_value("Wallet", wallet_name, "amount_delivered"))
	frappe.db.set_value(
		"Wallet",
		wallet_name,
		{"amount_received": ledger, "amount_remaining": ledger - delivered},
	)


@frappe.whitelist()
def reconcile_wallet(wallet_name, apply=0):
	"""Check the stored received / delivered / remaining against live GL and
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
	ledger = customer_gl_balance(w.customer)
	delivered = customer_delivered_total(w.customer)
	expected = {
		"amount_received": ledger,
		"amount_delivered": delivered,
		"amount_remaining": ledger - delivered,
	}
	stored = {k: flt(w.get(k)) for k in expected}
	in_sync = all(abs(stored[k] - flt(expected[k])) < 0.005 for k in expected)
	corrected = False
	if apply and not in_sync:
		recompute_from_deliveries(wallet_name, w.customer)
		corrected = True
	return {
		"customer": w.customer,
		"stored": stored,
		"expected": expected,
		"in_sync": in_sync,
		"corrected": corrected,
	}


# -- cross-doctype event handlers (wired in hooks.py doc_events) -------------


def enforce_wallet_balance(doc, method=None):
	"""Delivery Note `validate` (was "Delivery Note Wallet blocker", Before Save).

	Block a Delivery Note that would push the customer's wallet below zero --
	accounting for other open drafts already reserving the balance -- unless an
	active breach allowance covers the shortfall. On a hard block, raise a
	support Issue (in its own transaction so it survives the rollback) and throw.
	"""
	if not _wallet_enabled():
		return  # feature disabled in Fuelbuddy Settings -> never block on wallet balance
	wallet = frappe.db.get_value(
		"Wallet",
		{"customer": doc.customer, "payment_type": "Wallet"},
		[
			"name", "amount_received", "amount_remaining", "enable_breach", "breach_amount",
			"date_of_breach", "wallet_start_date",
		],
		as_dict=True,
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
	def _dn_sum(filters):
		return sum(flt(d.grand_total) for d in frappe.get_all("Delivery Note", filters=filters, fields=["grand_total"]))

	start = wallet.wallet_start_date
	committed = _dn_sum(_dn_filters(doc.customer, start, docstatus=1))
	other_drafts = _dn_sum(_dn_filters(doc.customer, start, docstatus=0, name=["!=", doc.name or ""]))
	available = flt(wallet.amount_received) - committed - other_drafts
	dn_amount = flt(doc.grand_total)

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
			"DN Amount: " + str(dn_amount) + "<br>"
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
	"""Delivery Note `on_update` (fires on draft saves and on submit).

	Refresh received / delivered / remaining on the customer's wallet by
	recomputing from the live Delivery Notes (docstatus 0/1)."""
	if not _wallet_enabled():
		return
	wallet = get_customer_wallet(doc.customer)
	if wallet:
		recompute_from_deliveries(wallet, doc.customer)


def update_wallet_on_delivery_note_cancel(doc, method=None):
	"""Delivery Note `on_cancel` / `after_delete`: the DN leaves the delivered
	set (docstatus 2, or gone), so the same total recompute drops its value and
	frees amount_remaining."""
	if not _wallet_enabled():
		return
	wallet = get_customer_wallet(doc.customer)
	if wallet:
		recompute_from_deliveries(wallet, doc.customer)


def _recompute_received_for_customer(customer):
	"""Shared body of the SI / PE handlers: both doctypes only move the customer's
	GL, so their submit AND cancel refresh received (and remaining) from GL."""
	if not _wallet_enabled():
		return
	wallet = get_customer_wallet(customer)
	if wallet:
		recompute_received(wallet)


def update_wallet_on_payment_entry_submit(doc, method=None):
	"""Payment Entry `on_submit` (was "Wallet amount update", After Submit)."""
	if doc.party_type == "Customer":
		_recompute_received_for_customer(doc.party)


def update_wallet_on_payment_entry_cancel(doc, method=None):
	"""Payment Entry `on_cancel`: the payment's GL entries are cancelled, so the
	wallet's received must drop — without this, a cancelled (e.g. bulk-cancelled)
	PE leaves amount_received inflated until an unrelated event recomputes."""
	if doc.party_type == "Customer":
		_recompute_received_for_customer(doc.party)


def update_wallet_on_sales_invoice_submit(doc, method=None):
	"""Sales Invoice `on_submit`: SI writes customer GL debits, which change the
	net GL balance the wallet's received is derived from."""
	if doc.get("customer"):
		_recompute_received_for_customer(doc.customer)


def update_wallet_on_sales_invoice_cancel(doc, method=None):
	"""Sales Invoice `on_cancel`: the SI's GL entries are cancelled; refresh."""
	if doc.get("customer"):
		_recompute_received_for_customer(doc.customer)


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
