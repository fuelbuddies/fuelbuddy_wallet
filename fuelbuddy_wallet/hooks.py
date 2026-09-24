app_name = "fuelbuddy_wallet"
app_title = "Fuelbuddy Wallet"
app_publisher = "Fuelbuddy"
app_description = "Wallet"
app_email = "shantanu.mishra@fuelbuddy.in"
app_license = "mit"

# Apps
# ------------------

# required_apps = []

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "fuelbuddy_wallet",
# 		"logo": "/assets/fuelbuddy_wallet/logo.png",
# 		"title": "Fuelbuddy Wallet",
# 		"route": "/fuelbuddy_wallet",
# 		"has_permission": "fuelbuddy_wallet.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/fuelbuddy_wallet/css/fuelbuddy_wallet.css"
# app_include_js = "/assets/fuelbuddy_wallet/js/fuelbuddy_wallet.js"

# include js, css files in header of web template
# web_include_css = "/assets/fuelbuddy_wallet/css/fuelbuddy_wallet.css"
# web_include_js = "/assets/fuelbuddy_wallet/js/fuelbuddy_wallet.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "fuelbuddy_wallet/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "fuelbuddy_wallet/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "fuelbuddy_wallet.utils.jinja_methods",
# 	"filters": "fuelbuddy_wallet.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "fuelbuddy_wallet.install.before_install"
# after_install = "fuelbuddy_wallet.install.after_install"

# Uninstallation
# ------------

# before_uninstall = "fuelbuddy_wallet.uninstall.before_uninstall"
# after_uninstall = "fuelbuddy_wallet.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "fuelbuddy_wallet.utils.before_app_install"
# after_app_install = "fuelbuddy_wallet.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "fuelbuddy_wallet.utils.before_app_uninstall"
# after_app_uninstall = "fuelbuddy_wallet.utils.after_app_uninstall"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "fuelbuddy_wallet.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# DocType Class
# ---------------
# Override standard doctype classes

# override_doctype_class = {
# 	"ToDo": "custom_app.overrides.CustomToDo"
# }

# Document Events
# ---------------
# Hook on document methods and events

# Wallet enforcement / balance upkeep, ported from DocType-Event Server Scripts.
# Handlers live on the Wallet controller; Frappe server-script event labels map
# to these hooks: "Before Save" -> validate, "After Save" -> on_update,
# "After Submit" -> on_submit.
_WALLET = "fuelbuddy_wallet.fuelbuddy_wallet.doctype.wallet.wallet"
doc_events = {
	# Per team convention: one submit-side and one cancel-side handler per doctype.
	"Delivery Note": {
		# before_save, NOT validate: Frappe runs validate on submit too, and a delivered
		# DN must never be stranded in draft by the wallet. before_save fires only for
		# draft insert / draft re-save (incl. amendments), which is exactly the punch path.
		"before_save": f"{_WALLET}.enforce_wallet_balance",
		"on_update": f"{_WALLET}.update_wallet_on_delivery_note",
		# Reverse a DN's wallet impact when it leaves the delivered set:
		#   on_cancel   -- submitted DN 1->2 (docstatus 2 no longer counted)
		#   after_delete-- draft DN deleted (e.g. backend order-cancel deletes the draft)
		"on_cancel": f"{_WALLET}.update_wallet_on_delivery_note_cancel",
		"after_delete": f"{_WALLET}.update_wallet_on_delivery_note_cancel",
	},
	# SI and PE both move the customer's GL, which received is derived from —
	# submit AND cancel each refresh the wallet (bulk cancel included).
	"Sales Invoice": {
		"on_submit": f"{_WALLET}.update_wallet_on_sales_invoice_submit",
		"on_cancel": f"{_WALLET}.update_wallet_on_sales_invoice_cancel",
	},
	"Payment Entry": {
		"on_submit": f"{_WALLET}.update_wallet_on_payment_entry_submit",
		"on_cancel": f"{_WALLET}.update_wallet_on_payment_entry_cancel",
	},
	"Customer": {
		"after_insert": f"{_WALLET}.create_wallet_for_customer",
	},
}

# Scheduled Tasks
# ---------------
# Expire breaches ~12h after they were enabled (window measured per-wallet from
# date_of_breach). Runs hourly so each wallet closes within ~1h of its 12h mark.
scheduler_events = {
	"hourly": [
		f"{_WALLET}.close_expired_breaches",
	],
}

# scheduler_events = {
# 	"all": [
# 		"fuelbuddy_wallet.tasks.all"
# 	],
# 	"daily": [
# 		"fuelbuddy_wallet.tasks.daily"
# 	],
# 	"hourly": [
# 		"fuelbuddy_wallet.tasks.hourly"
# 	],
# 	"weekly": [
# 		"fuelbuddy_wallet.tasks.weekly"
# 	],
# 	"monthly": [
# 		"fuelbuddy_wallet.tasks.monthly"
# 	],
# }

# Testing
# -------

# before_tests = "fuelbuddy_wallet.install.before_tests"

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "fuelbuddy_wallet.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "fuelbuddy_wallet.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["fuelbuddy_wallet.utils.before_request"]
# after_request = ["fuelbuddy_wallet.utils.after_request"]

# Job Events
# ----------
# before_job = ["fuelbuddy_wallet.utils.before_job"]
# after_job = ["fuelbuddy_wallet.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"fuelbuddy_wallet.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []

