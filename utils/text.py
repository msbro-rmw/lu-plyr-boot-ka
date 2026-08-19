"""
utils/text.py — shared text helpers.

display_title():
  Class "name" (jo generate karte time daala jaata hai) URL-safe slug hota hai
  (spaces -> hyphens, sirf letters/numbers/hyphen). Lekin jahan bhi ye title
  DIKHANA ho (player page header, Telegram caption) — wahan hyphen(-) aur
  underscore(_) ko wapas simple SPACE se replace karke clean readable title
  dikhana hai. Ye function hi single source of truth hai taaki har jagah
  (Flask templates + recorder.py + Telegram bot) exact same conversion ho.

sanitize_name():
  Website form aur Telegram bot dono jagah se aane wale "class name / video
  titel" ko is EK function se guzaarte hain, taaki dono jagah exact same
  URL-safe slug bane. Spaces -> hyphen(-). Hindi + English letters aur
  numbers allowed (unicodedata category L/N — isliye हिंदी जैसे titles bhi
  automatic support ho jaate hain, koi extra kaam nahi karna padta).
  Special characters (:,+,|| etc.) ko silently strip karke unki jagah space
  laga di jaati hai (phir wo space hyphen ban jaata hai) — kabhi error nahi
  deta. NO LENGTH LIMIT (titel kitna bhi lamba ho sakta hai).
"""
import re
import unicodedata


def display_title(name: str) -> str:
    if not name:
        return ""
    # hyphen aur underscore (ek ya zyada continuous) -> single space
    title = re.sub(r"[-_]+", " ", str(name))
    title = re.sub(r"\s+", " ", title).strip()
    return title


def sanitize_name(name: str) -> str:
    """Spaces -> hyphens; sirf letters (kisi bhi language incl. Hindi),
    numbers aur hyphen rakho. Baaki har special character (:,+,|| etc.)
    silently ek space se replace ho jaata hai (jo aage hyphen ban jaata
    hai) — kabhi bhi error nahi, bas parse/clean ho jaata hai.
    NO LENGTH LIMIT.

    IMPORTANT (Hindi support): sirf category "L" (letter) aur "N" (number)
    rakhna kaafi NAHI hai — Hindi/Devanagari words matras (vowel signs jaise
    ा ि ी ु ू े ै ो ौ), anusvara (ं), chandrabindu (ँ) aur virama/halant (्)
    par depend karte hain, jo Unicode category "Mn"/"Mc" (combining marks)
    mein aate hain, "L"/"N" mein nahi. Agar inhe strip kar diya jaaye to
    "का" जैसा simple word bhi "क" bन जाता है (matra gayab) — isliye category
    "M" (Mn/Mc/Me) bhi explicitly allowed hai taaki har Hindi word (जैसे
    "का हवाओं ही") sahi se poora connect ho.
    """
    name = (name or "").strip()
    # Pehle: kisi bhi character jo letter/mark/number/space/hyphen NAHI hai
    # (jaise :,+,|| etc.) usko space se replace karo — reject/error nahi.
    cleaned = []
    for ch in name:
        if ch == "-" or ch.isspace():
            cleaned.append(" ")
        elif unicodedata.category(ch)[0] in ("L", "M", "N"):
            cleaned.append(ch)
        else:
            cleaned.append(" ")
    name = "".join(cleaned)
    name = re.sub(r"\s+", "-", name.strip())
    slug = re.sub(r"-{2,}", "-", name).strip("-")
    return slug
