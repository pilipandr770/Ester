"""Stripe: подписка 10 €/мес через Checkout + портал управления."""
import json

import stripe
from config import Config
from models import db, User, StripeEvent

stripe.api_key = Config.STRIPE_SECRET_KEY

LIVE_STATUSES = ("active", "trialing", "past_due")
DEAD_STATUSES = ("canceled", "incomplete_expired")


def checkout_url(user):
    if not user.stripe_customer_id:
        c = stripe.Customer.create(email=user.email, metadata={"user_id": user.id})
        user.stripe_customer_id = c.id
        db.session.commit()
    params = dict(
        mode="subscription",
        customer=user.stripe_customer_id,
        line_items=[{"price": Config.STRIPE_PRICE_ID, "quantity": 1}],
        client_reference_id=str(user.id),
        success_url=f"{Config.BASE_URL}/billing?ok=1",
        cancel_url=f"{Config.BASE_URL}/billing",
        allow_promotion_codes=True,
    )
    if Config.STRIPE_AUTOMATIC_TAX:  # НДС: нужен настроенный Stripe Tax
        params.update(
            automatic_tax={"enabled": True},
            billing_address_collection="required",
            tax_id_collection={"enabled": True},
            customer_update={"address": "auto", "name": "auto"},
        )
    return stripe.checkout.Session.create(**params).url


def portal_url(user):
    s = stripe.billing_portal.Session.create(
        customer=user.stripe_customer_id, return_url=f"{Config.BASE_URL}/billing")
    return s.url


def cancel_subscriptions(user):
    """Отменяет все живые подписки клиента (вызывается перед удалением аккаунта).
    Сам Stripe-клиент остаётся: он нужен для счетов и бухгалтерии."""
    if not user.stripe_customer_id:
        return
    subs = stripe.Subscription.list(customer=user.stripe_customer_id, status="all", limit=100)
    for s in subs.data:
        if s["status"] not in DEAD_STATUSES:
            stripe.Subscription.cancel(s["id"])


def _sync_subscription(user, sub_id):
    """Берём актуальное состояние подписки у Stripe: так порядок прихода событий не важен."""
    status = stripe.Subscription.retrieve(sub_id)["status"]
    if user.stripe_subscription_id and user.stripe_subscription_id != sub_id:
        # событие старой подписки не должно затирать новую
        if status not in ("active", "trialing"):
            return
    user.stripe_subscription_id = sub_id
    user.sub_status = status


def handle_event(payload, sig):
    stripe.Webhook.construct_event(payload, sig, Config.STRIPE_WEBHOOK_SECRET)  # проверка подписи
    ev = json.loads(payload)
    t, obj = ev["type"], ev["data"]["object"]

    if db.session.get(StripeEvent, ev["id"]):
        return t  # повторная доставка

    if t == "checkout.session.completed":
        u = db.session.get(User, int(obj.get("client_reference_id") or 0))
        if u and obj.get("subscription"):
            u.stripe_customer_id = obj.get("customer") or u.stripe_customer_id
            _sync_subscription(u, obj["subscription"])
    elif t in ("customer.subscription.created", "customer.subscription.updated",
               "customer.subscription.deleted"):
        u = User.query.filter_by(stripe_customer_id=obj.get("customer")).first()
        if u:
            _sync_subscription(u, obj["id"])
    elif t == "invoice.payment_failed":
        u = User.query.filter_by(stripe_customer_id=obj.get("customer")).first()
        if u and u.sub_status == "active":
            u.sub_status = "past_due"

    db.session.add(StripeEvent(id=ev["id"]))
    db.session.commit()
    return t
