import keyboard

print("Pulsa cualquier tecla para ver su nombre y scan_code. Pulsa Esc para salir.")


def on_press(event):
    print(f"name={event.name!r}  scan_code={event.scan_code}  is_keypad={event.is_keypad}")


keyboard.on_press(on_press)
keyboard.wait("esc")
