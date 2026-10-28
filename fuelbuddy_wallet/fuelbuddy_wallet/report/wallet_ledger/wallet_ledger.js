// Copyright (c) 2026, Fuelbuddy and contributors
// For license information, please see license.txt

frappe.query_reports["Wallet Ledger"] = {
	filters: [
		{
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			default: frappe.defaults.get_user_default("Company"),
			reqd: 1,
		},
		{
			fieldname: "from_date",
			label: __("From Date"),
			fieldtype: "Date",
			default: frappe.datetime.add_months(frappe.datetime.get_today(), -1),
			reqd: 1,
		},
		{
			fieldname: "to_date",
			label: __("To Date"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
			reqd: 1,
		},
		{ fieldname: "customer", label: __("Customer"), fieldtype: "Link", options: "Customer" },
		{ fieldname: "voucher_type", label: __("Voucher Type"), fieldtype: "Link", options: "DocType" },
		{ fieldname: "voucher_no", label: __("Voucher No"), fieldtype: "Data" },
		{ fieldname: "show_cancelled_entries", label: __("Show Cancelled Entries"), fieldtype: "Check" },
		{ fieldname: "submitted_only", label: __("Submitted Only"), fieldtype: "Check" },
	],
};
