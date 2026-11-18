import hashlib
import html
import json
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

SALT = b"Lee"  # 소금
DBMS_URL = "http://127.0.0.1:8080/query"  # 미니 DBMS API 서버


def hash_pw(pw):
    # 비밀번호 + 소금 "Lee" 를 섞어서 10만 번 반복 해시 (되돌릴 수 없음)
    return hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), SALT, 100_000).hex()


def lit(s):
    # SQL 문자열 값으로 안전하게: ' 는 '' 로
    return "'" + s.replace("'", "''") + "'"


def q(sql):
    # 미니 DBMS 서버에 SQL을 보내고 첫 번째 결과를 돌려받음
    req = urllib.request.Request(
        DBMS_URL,
        data=json.dumps({"sql": sql}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())["results"][0]
    except urllib.error.HTTPError as e:
        raise RuntimeError(json.loads(e.read()).get("error", "DBMS error"))
    except urllib.error.URLError:
        raise RuntimeError("DBMS 서버(8080)에 연결할 수 없습니다")


FORMS = """<h3>회원가입</h3>
<form method="post" action="/signup">id <input name="id"> pw <input name="pw" type="password"> <button>가입</button></form>
<h3>로그인</h3>
<form method="post" action="/login">id <input name="id"> pw <input name="pw" type="password"> <button>로그인</button></form>"""


def page(msg="", sql="", table=""):
    out = "<meta charset='utf-8'>" + FORMS + "<p>" + html.escape(msg) + "</p>"
    if sql:
        out += "<p>DBMS로 보낸 SQL: <code>" + html.escape(sql) + "</code></p>"
    return out + table


def table(res):
    head = "".join("<th>%s</th>" % html.escape(c) for c in res["columns"])
    rows = "".join(
        "<tr>" + "".join("<td>%s</td>" % html.escape(str(v)) for v in row) + "</tr>"
        for row in res["rows"]
    )
    return "<table border='1'><tr>" + head + "</tr>" + rows + "</table>"


class Handler(BaseHTTPRequestHandler):
    def send(self, body):
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.send(page())

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        form = parse_qs(self.rfile.read(n).decode("utf-8"))
        uid = form.get("id", [""])[0].strip()
        pw = form.get("pw", [""])[0]
        if not uid or not pw:
            return self.send(page("id와 pw를 입력하세요"))
        try:
            if self.path == "/signup":
                found = q("SELECT uid FROM accounts WHERE uid = " + lit(uid))
                if found["rows"]:
                    return self.send(page("이미 있는 id입니다"))
                sql = "INSERT INTO accounts (uid, pw) VALUES (%s, %s)" % (lit(uid), lit(hash_pw(pw)))
                q(sql)
                return self.send(page("가입 완료: " + uid, sql))
            if self.path == "/login":
                sql = "SELECT pw FROM accounts WHERE uid = " + lit(uid)
                rows = q(sql)["rows"]
                if rows and rows[0][0] == hash_pw(pw):
                    return self.send(page("로그인 성공", sql, table(q("SELECT * FROM accounts"))))
                return self.send(page("로그인 실패", sql))
        except RuntimeError as e:
            return self.send(page("오류: " + str(e)))
        self.send(page())


if __name__ == "__main__":
    # 처음 한 번만: 테이블이 비어 있으면 aaa/bbb 를 DBMS에 넣음
    if q("SELECT COUNT(*) FROM accounts")["rows"][0][0] == 0:
        q("INSERT INTO accounts (uid, pw) VALUES (%s, %s), (%s, %s)"
          % (lit("aaa"), lit(hash_pw("1111")), lit("bbb"), lit(hash_pw("2222"))))
    print("http://127.0.0.1:8000")
    HTTPServer(("127.0.0.1", 8000), Handler).serve_forever()
