from telegram import Update, LabeledPrice
from telegram.ext import ContextTypes

import entitlements

PRODUCTS = {
    "cardio_module": ("Cardiovascular Pharmacology", 200),
    "full_30d": ("Full Access – 30 Days", 500),
    "lifetime": ("Lifetime Unlimited", 2000),
}


async def buy_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from bot_quiz_interactive import guard
    if not await guard(update):
        return
    args = ctx.args
    if not args or args[0] not in PRODUCTS:
        options = "\n".join(f"/buy {key} — {stars} ⭐ ({title})" for key, (title, stars) in PRODUCTS.items())
        await update.message.reply_text(f"Usage: /buy <product>\n\n{options}")
        return
    product = args[0]
    title, stars = PRODUCTS[product]
    await ctx.bot.send_invoice(
        chat_id=update.effective_chat.id,
        title=title,
        description=f"Unlocks {title.lower()} in the study bot.",
        payload=product,
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(title, stars)],
    )


async def precheckout_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    if query.invoice_payload in PRODUCTS:
        await query.answer(ok=True)
    else:
        await query.answer(ok=False, error_message="Unknown product — please try /buy again.")


async def successful_payment_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    payment = update.message.successful_payment
    product = payment.invoice_payload
    user_id = update.effective_user.id
    entitlements.grant_entitlement(user_id, product, payment.telegram_payment_charge_id)
    title, _ = PRODUCTS.get(product, (product, 0))
    await update.message.reply_text(f"✅ Purchase confirmed — {title} unlocked. Thank you!")
