from datetime import datetime

from typer.testing import CliRunner

from relevamiento.cli import app, en_horario_laboral

runner = CliRunner()


def repos_csv(tmp_path):
    ruta = tmp_path / "repos.csv"
    ruta.write_text("id,nombre,url_base,oai_endpoint\nr1,Uno,https://uno.org/,\nr2,Dos,https://dos.org/,https://dos.org/oai\n")
    return ruta


def test_plan_no_necesita_contacto_ni_hace_pedidos(tmp_path):
    resultado = runner.invoke(app, ["relevar", str(repos_csv(tmp_path)), "--plan", "--solo", "r2"])
    assert resultado.exit_code == 0, resultado.output
    assert "https://dos.org/oai?verb=Identify" in resultado.output
    assert "uno.org" not in resultado.output


def test_relevar_exige_contacto(tmp_path, monkeypatch):
    monkeypatch.delenv("RELEVAMIENTO_CONTACTO", raising=False)
    resultado = runner.invoke(app, ["relevar", str(repos_csv(tmp_path)), "--salida", str(tmp_path / "s")])
    assert resultado.exit_code != 0 and "contacto" in resultado.output
    assert not (tmp_path / "s").exists()


def test_solo_con_id_desconocido(tmp_path):
    resultado = runner.invoke(app, ["relevar", str(repos_csv(tmp_path)), "--plan", "--solo", "zz"])
    assert resultado.exit_code != 0 and "zz" in resultado.output


def test_filas_vacias_se_ignoran(tmp_path):
    ruta = tmp_path / "repos.csv"
    ruta.write_text("id,nombre,url_base,oai_endpoint\nr1,Uno,https://uno.org/,\n,,,\n,,\n")
    resultado = runner.invoke(app, ["relevar", str(ruta), "--plan"])
    assert resultado.exit_code == 0, resultado.output
    assert "uno.org" in resultado.output


def test_fila_incompleta_sigue_siendo_error(tmp_path):
    ruta = tmp_path / "repos.csv"
    ruta.write_text("id,nombre,url_base,oai_endpoint\nr1,Uno,,\n")
    resultado = runner.invoke(app, ["relevar", str(ruta), "--plan"])
    assert resultado.exit_code != 0 and "url_base" in resultado.output


def test_csv_invalido(tmp_path):
    ruta = tmp_path / "malo.csv"
    ruta.write_text("id,nombre\nr1,Uno\n")
    resultado = runner.invoke(app, ["relevar", str(ruta), "--plan"])
    assert resultado.exit_code != 0 and "url_base" in resultado.output


def test_horario_laboral():
    assert en_horario_laboral(datetime(2026, 10, 28, 10, 0))  # miércoles
    assert not en_horario_laboral(datetime(2026, 10, 28, 20, 0))
    assert not en_horario_laboral(datetime(2026, 10, 31, 10, 0))  # sábado


def test_verificar_sin_pedidos_da_ok(tmp_path):
    resultado = runner.invoke(app, ["verificar-cortesia", "--salida", str(tmp_path)])
    assert resultado.exit_code == 0 and "OK" in resultado.output
