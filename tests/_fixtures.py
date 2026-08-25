"""Constructores de despachos de prueba, con la forma que entrega el backend."""


def make_point(pid=684, name="Y DE CARIGÁN", latitude=-4.0100, longitude=-79.2294, radius=50):
    return {"id": pid, "name": name, "latitude": latitude, "longitude": longitude, "radius": radius}


def make_checkpoint(cid=3701, pid=684, order=0, time_calculated="06:30:00",
                    time_reported="00:00:00", point=None):
    return {
        "id": cid,
        "order": order,
        "time": "00:00:00",
        "time_calculated": time_calculated,
        "time_reported": time_reported,
        "point": make_point(pid) if point is None else point,
    }


def make_line(number=8, name="A2"):
    return {"id": 17, "name": name, "number": number,
            "start_route": "CARIGAN", "end_route": "CIUDAD VICTORIA"}


def make_step(step=1, start="06:00:00", end="07:00:00", checkpoints=None, line=None):
    if checkpoints is None:
        checkpoints = [
            make_checkpoint(3701, 684, 0, "06:10:00"),
            make_checkpoint(3702, 685, 1, "06:30:00"),
            make_checkpoint(3703, 686, 2, "06:50:00"),
        ]
    return {
        "step": step,
        "code": "G807",
        "register": 1624,
        "start_schedule": start,
        "end_schedule": end,
        "line": make_line() if line is None else line,
        "checkpoints": checkpoints,
    }
