"""Email sending via Resend."""

import logging

import resend

from src.utils.config import get_settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared email layout
# ---------------------------------------------------------------------------

_SCALES_SVG = (
    '<svg width="28" height="28" viewBox="0 0 24 24" fill="none" '
    'xmlns="http://www.w3.org/2000/svg" stroke="#ffffff" stroke-width="2" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M12 2 L10.5 5 L13.5 5 Z" fill="#ffffff" stroke="none"/>'
    '<line x1="4" y1="5" x2="20" y2="5"/>'
    '<circle cx="4" cy="4.2" r="0.6" fill="#ffffff" stroke="none"/>'
    '<circle cx="20" cy="4.2" r="0.6" fill="#ffffff" stroke="none"/>'
    '<path d="M4 5 L2 13 C2 13 4 15 6 15 C8 15 10 13 10 13 L8 5"/>'
    '<path d="M16 5 L14 13 C14 13 16 15 18 15 C20 15 22 13 22 13 L20 5"/>'
    '<line x1="12" y1="5" x2="12" y2="18"/>'
    '<line x1="12" y1="18" x2="8" y2="22"/>'
    '<line x1="12" y1="18" x2="16" y2="22"/>'
    '<line x1="8" y1="22" x2="16" y2="22"/>'
    '</svg>'
)


def _email_wrapper(body_html: str) -> str:
    """Wrap email body in the shared Intention layout."""
    return f"""\
<!DOCTYPE html>
<html lang="fr">
<head><meta charset="UTF-8"></head>
<body style="margin:0; padding:0; background-color:#f8fafc; font-family:Inter,-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#f8fafc; padding:48px 16px;">
    <tr>
      <td align="center">
        <table role="presentation" width="560" cellpadding="0" cellspacing="0" style="background-color:#ffffff; border-radius:16px; overflow:hidden; box-shadow:0 4px 12px rgba(15,23,42,0.06);">
          <!-- Header -->
          <tr>
            <td style="background-color:#1e3a5f; padding:32px 40px; text-align:center;">
              <table role="presentation" cellpadding="0" cellspacing="0" style="margin:0 auto;">
                <tr>
                  <td style="vertical-align:middle; padding-right:12px;">
                    {_SCALES_SVG}
                  </td>
                  <td style="vertical-align:middle;">
                    <span style="color:#ffffff; font-size:20px; font-weight:600; letter-spacing:-0.4px;">Intention</span>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
          <!-- Body -->
          <tr>
            <td style="padding:40px; color:#0f172a;">
              {body_html}
            </td>
          </tr>
          <!-- Footer -->
          <tr>
            <td style="padding:0 40px 32px;">
              <p style="margin:0; font-size:12px; color:#94a3b8; line-height:1.6;">
                Intention - Analyse de l'intention du législateur<br>
                <span style="color:#cbd5e1;">DUSA LABS · Paris</span>
              </p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


def _cta_button(href: str, label: str) -> str:
    """Return a table-based CTA button (email-client compatible)."""
    return f"""\
<table role="presentation" cellpadding="0" cellspacing="0" style="margin:28px 0;">
  <tr>
    <td style="background-color:#1e3a5f; border-radius:10px;">
      <a href="{href}"
         style="display:inline-block; padding:14px 28px; color:#ffffff; font-size:15px; font-weight:500; text-decoration:none; letter-spacing:-0.1px;">
        {label}
      </a>
    </td>
  </tr>
</table>"""


# ---------------------------------------------------------------------------
# Email senders
# ---------------------------------------------------------------------------

def send_reset_email(to_email: str, reset_url: str) -> bool:
    """Send a password reset email. Returns True on success."""
    settings = get_settings()
    if not settings.resend_api_key:
        logger.error("RESEND_API_KEY not configured")
        return False

    resend.api_key = settings.resend_api_key

    body = f"""\
<h1 style="margin:0 0 12px; font-size:24px; font-weight:600; color:#0f172a; letter-spacing:-0.3px;">
  Réinitialisez votre mot de passe
</h1>
<p style="margin:0 0 8px; font-size:15px; line-height:1.7; color:#475569;">
  Utilisez le lien ci-dessous pour définir un nouveau mot de passe. Il reste valide pendant une heure.
</p>
{_cta_button(reset_url, "Choisir un nouveau mot de passe")}
<p style="margin:0 0 8px; font-size:13px; color:#94a3b8;">
  Si le bouton ne fonctionne pas, copiez ce lien dans votre navigateur :
</p>
<p style="margin:0 0 28px; font-size:13px; color:#1e3a5f; word-break:break-all;">
  {reset_url}
</p>
<p style="margin:0; font-size:13px; line-height:1.6; color:#94a3b8;">
  Vous n'êtes pas à l'origine de cette demande ? Cet email peut être ignoré sans risque.
</p>"""

    try:
        resend.Emails.send({
            "from": "Intention <send@contact.intention-legal.fr>",
            "to": [to_email],
            "subject": "Réinitialiser votre mot de passe",
            "html": _email_wrapper(body),
        })
        return True
    except Exception as e:
        logger.error(f"Failed to send reset email: {e}")
        return False


def send_article_added_email(to_email: str, article_ref: str) -> bool:
    """Notify a user that a reported article has been added."""
    settings = get_settings()
    if not settings.resend_api_key:
        return False

    resend.api_key = settings.resend_api_key
    frontend_url = settings.frontend_url

    body = f"""\
<h1 style="margin:0 0 12px; font-size:24px; font-weight:600; color:#0f172a; letter-spacing:-0.3px;">
  Votre article est disponible
</h1>
<p style="margin:0; font-size:15px; line-height:1.7; color:#475569;">
  L'article <strong style="color:#0f172a;">{article_ref}</strong> que vous aviez signalé vient d'être ajouté à la base. Son analyse est désormais consultable.
</p>
{_cta_button(f"{frontend_url}/results/article/{article_ref}", "Voir l'analyse")}"""

    try:
        resend.Emails.send({
            "from": "Intention <send@contact.intention-legal.fr>",
            "to": [to_email],
            "subject": f"L'article {article_ref} est disponible",
            "html": _email_wrapper(body),
        })
        return True
    except Exception as e:
        logger.error(f"Failed to send article added email: {e}")
        return False


def send_contact_notification_email(admin_email: str, from_email: str, message: str) -> bool:
    """Notify admin of a new contact message."""
    settings = get_settings()
    if not settings.resend_api_key:
        return False

    resend.api_key = settings.resend_api_key

    body = f"""\
<h1 style="margin:0 0 12px; font-size:24px; font-weight:600; color:#0f172a; letter-spacing:-0.3px;">
  Nouveau message de contact
</h1>
<p style="margin:0 0 20px; font-size:15px; line-height:1.7; color:#475569;">
  <strong style="color:#0f172a;">De :</strong> {from_email}
</p>
<div style="background:#f8fafc; padding:20px; border-radius:10px; border:1px solid #e2e8f0; margin:0;">
  <p style="margin:0; white-space:pre-wrap; color:#0f172a; font-size:15px; line-height:1.7;">{message}</p>
</div>"""

    try:
        resend.Emails.send({
            "from": "Intention <send@contact.intention-legal.fr>",
            "to": [admin_email],
            "subject": f"Nouveau message de contact - {from_email}",
            "html": _email_wrapper(body),
        })
        return True
    except Exception as e:
        logger.error(f"Failed to send contact notification: {e}")
        return False


def send_welcome_email(to_email: str, name: str) -> bool:
    """Send a welcome email to a new user."""
    settings = get_settings()
    if not settings.resend_api_key:
        return False

    resend.api_key = settings.resend_api_key
    frontend_url = settings.frontend_url
    greeting = f"Bienvenue, {name}" if name else "Bienvenue"

    body = f"""\
<h1 style="margin:0 0 12px; font-size:24px; font-weight:600; color:#0f172a; letter-spacing:-0.3px;">
  {greeting}
</h1>
<p style="margin:0; font-size:15px; line-height:1.7; color:#475569;">
  Intention analyse l'intention du législateur derrière les articles du Code général des impôts - contexte, débats parlementaires, amendements. Tapez une référence pour commencer.
</p>
{_cta_button(f"{frontend_url}/search", "Explorer un article")}"""

    try:
        resend.Emails.send({
            "from": "Intention <send@contact.intention-legal.fr>",
            "to": [to_email],
            "subject": "Votre compte Intention est prêt",
            "html": _email_wrapper(body),
        })
        return True
    except Exception as e:
        logger.error(f"Failed to send welcome email: {e}")
        return False


def send_email_verification_email(to_email: str, verify_url: str) -> bool:
    """Send the signup verification link - lands on /verify-email?token=..."""
    settings = get_settings()
    if not settings.resend_api_key:
        return False

    resend.api_key = settings.resend_api_key

    body = f"""\
<h1 style="margin:0 0 12px; font-size:24px; font-weight:600; color:#0f172a; letter-spacing:-0.3px;">
  Encore une étape
</h1>
<p style="margin:0; font-size:15px; line-height:1.7; color:#475569;">
  Confirmez votre adresse pour activer votre compte Intention. Le lien est valide 24 heures.
</p>
{_cta_button(verify_url, "Confirmer mon adresse")}
<p style="margin:0; font-size:13px; line-height:1.6; color:#94a3b8;">
  Vous n'avez pas créé de compte ? Ignorez cet email.
</p>"""

    try:
        resend.Emails.send({
            "from": "Intention <send@contact.intention-legal.fr>",
            "to": [to_email],
            "subject": "Confirmer votre adresse email",
            "html": _email_wrapper(body),
        })
        return True
    except Exception as e:
        logger.error(f"Failed to send verification email: {e}")
        return False


def send_email_change_email(to_email: str, verify_url: str) -> bool:
    """Send the confirm-new-email link to the NEW email address."""
    settings = get_settings()
    if not settings.resend_api_key:
        return False

    resend.api_key = settings.resend_api_key

    body = f"""\
<h1 style="margin:0 0 12px; font-size:24px; font-weight:600; color:#0f172a; letter-spacing:-0.3px;">
  Confirmez votre nouvelle adresse
</h1>
<p style="margin:0; font-size:15px; line-height:1.7; color:#475569;">
  Vous avez demandé à changer l'adresse email de votre compte Intention. Validez ce changement ci-dessous. Le lien expire dans une heure.
</p>
{_cta_button(verify_url, "Valider cette adresse")}
<p style="margin:0; font-size:13px; line-height:1.6; color:#94a3b8;">
  Vous n'avez rien demandé ? Cet email peut être ignoré.
</p>"""

    try:
        resend.Emails.send({
            "from": "Intention <send@contact.intention-legal.fr>",
            "to": [to_email],
            "subject": "Valider votre nouvelle adresse email",
            "html": _email_wrapper(body),
        })
        return True
    except Exception as e:
        logger.error(f"Failed to send email-change email: {e}")
        return False


def send_feedback_reply_email(
    to_email: str,
    article_ref: str,
    original_message: str,
    reply_message: str,
    section_index: int | None = None,
) -> bool:
    """Send a reply notification for a feedback/comment left on an article."""
    settings = get_settings()
    if not settings.resend_api_key:
        return False

    resend.api_key = settings.resend_api_key
    frontend_url = settings.frontend_url

    article_url = f"{frontend_url}/results/article/{article_ref}"
    if section_index is not None:
        article_url += f"#toc-section-{section_index}"

    original_block = (
        f"""<p style="color:#64748b; font-size:12px; font-weight:600; text-transform:uppercase; letter-spacing:0.5px; margin:0 0 8px;">Votre retour</p>
<div style="background:#f8fafc; padding:16px 20px; border-radius:10px; border:1px solid #e2e8f0; margin:0 0 20px;">
  <p style="margin:0; white-space:pre-wrap; color:#475569; font-size:14px; line-height:1.7;">{original_message}</p>
</div>"""
        if original_message
        else ""
    )

    body = f"""\
<h1 style="margin:0 0 12px; font-size:24px; font-weight:600; color:#0f172a; letter-spacing:-0.3px;">
  Réponse à votre retour
</h1>
<p style="margin:0 0 20px; font-size:15px; line-height:1.7; color:#475569;">
  Votre retour sur l'article <strong style="color:#0f172a;">{article_ref}</strong> a reçu une réponse.
</p>
{original_block}
<p style="color:#1e3a5f; font-size:12px; font-weight:600; text-transform:uppercase; letter-spacing:0.5px; margin:0 0 8px;">Notre réponse</p>
<div style="background:#eff6ff; padding:16px 20px; border-radius:10px; border-left:3px solid #1e3a5f; margin:0;">
  <p style="margin:0; white-space:pre-wrap; color:#0f172a; font-size:15px; line-height:1.7;">{reply_message}</p>
</div>
{_cta_button(article_url, "Voir l'article")}"""

    try:
        resend.Emails.send({
            "from": "Intention <send@contact.intention-legal.fr>",
            "to": [to_email],
            "subject": f"Réponse à votre retour sur l'article {article_ref}",
            "html": _email_wrapper(body),
        })
        return True
    except Exception as e:
        logger.error(f"Failed to send feedback reply email: {e}")
        return False


def send_contact_reply_email(to_email: str, original_message: str, reply_message: str, contact_id: int = 0) -> bool:
    """Send a reply notification to a contact message."""
    settings = get_settings()
    if not settings.resend_api_key:
        return False

    resend.api_key = settings.resend_api_key
    frontend_url = settings.frontend_url

    body = f"""\
<h1 style="margin:0 0 20px; font-size:24px; font-weight:600; color:#0f172a; letter-spacing:-0.3px;">
  Réponse à votre message
</h1>
<p style="color:#64748b; font-size:12px; font-weight:600; text-transform:uppercase; letter-spacing:0.5px; margin:0 0 8px;">Votre message</p>
<div style="background:#f8fafc; padding:16px 20px; border-radius:10px; border:1px solid #e2e8f0; margin:0 0 20px;">
  <p style="margin:0; white-space:pre-wrap; color:#475569; font-size:14px; line-height:1.7;">{original_message}</p>
</div>
<p style="color:#1e3a5f; font-size:12px; font-weight:600; text-transform:uppercase; letter-spacing:0.5px; margin:0 0 8px;">Notre réponse</p>
<div style="background:#eff6ff; padding:16px 20px; border-radius:10px; border-left:3px solid #1e3a5f; margin:0;">
  <p style="margin:0; white-space:pre-wrap; color:#0f172a; font-size:15px; line-height:1.7;">{reply_message}</p>
</div>
{_cta_button(f"{frontend_url}/contact?ticket={contact_id}", "Voir la conversation")}
<p style="color:#94a3b8; font-size:13px; line-height:1.6; margin:0;">
  Pour poursuivre l'échange, répondez depuis votre espace Intention.
</p>"""

    try:
        resend.Emails.send({
            "from": "Intention <send@contact.intention-legal.fr>",
            "to": [to_email],
            "subject": "Réponse à votre message",
            "html": _email_wrapper(body),
        })
        return True
    except Exception as e:
        logger.error(f"Failed to send contact reply email: {e}")
        return False
