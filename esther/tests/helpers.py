import re


def csrf_token(client, path="/login"):
    html = client.get(path).get_data(as_text=True)
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def post(client, path, data=None, form_page="/login", **kw):
    data = dict(data or {})
    data["csrf_token"] = csrf_token(client, form_page)
    return client.post(path, data=data, **kw)
