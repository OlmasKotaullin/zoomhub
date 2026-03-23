"""Тесты маршрутов."""


def test_index(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "ZoomHub" in response.text


def test_create_folder(client):
    response = client.post("/folders", data={"name": "Разработка", "icon": "💻"})
    assert response.status_code == 200
    assert "Разработка" in response.text


def test_delete_folder_not_found(client):
    response = client.delete("/folders/999")
    assert response.status_code == 404


def test_folder_detail_not_found(client):
    response = client.get("/folders/999")
    assert response.status_code == 404


def test_meeting_detail_not_found(client):
    response = client.get("/meetings/999")
    assert response.status_code == 404


def test_search_empty(client):
    response = client.get("/meetings/search?q=")
    assert response.status_code == 200
