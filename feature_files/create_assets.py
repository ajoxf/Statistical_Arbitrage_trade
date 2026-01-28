"""Create placeholder assets for the installer"""
import struct
import os

# Create assets directory if it doesn't exist
os.makedirs('assets', exist_ok=True)

# Create icon.ico
if not os.path.exists('assets/icon.ico'):
    print('       Creating placeholder icon...')
    ico_header = struct.pack('<HHH', 0, 1, 1)
    ico_entry = struct.pack('<BBBBHHII', 16, 16, 0, 0, 1, 32, 40+16*16*4, 22)
    bmp_header = struct.pack('<IiiHHIIiiII', 40, 16, 32, 1, 32, 0, 16*16*4, 0, 0, 0, 0)
    pixels = b'\x60\x45\xe9\xff' * 16 * 16
    with open('assets/icon.ico', 'wb') as f:
        f.write(ico_header + ico_entry + bmp_header + pixels)
    print('       Icon created')

# Create wizard_large.bmp
if not os.path.exists('assets/wizard_large.bmp'):
    print('       Creating wizard large image...')
    w, h = 164, 314
    row_size = (w * 3 + 3) // 4 * 4
    img_size = row_size * h
    file_size = 54 + img_size
    header = struct.pack('<2sIHHI', b'BM', file_size, 0, 0, 54)
    info = struct.pack('<IiiHHIIiiII', 40, w, h, 1, 24, 0, img_size, 0, 0, 0, 0)
    rows = []
    for y in range(h):
        row = b''
        for x in range(w):
            b = int(30 + (y / h) * 20)
            g = int(26 + (y / h) * 15)
            r = int(46 + (y / h) * 25)
            row += bytes([b, g, r])
        row += b'\x00' * (row_size - w * 3)
        rows.append(row)
    with open('assets/wizard_large.bmp', 'wb') as f:
        f.write(header + info + b''.join(rows))
    print('       Wizard large image created')

# Create wizard_small.bmp
if not os.path.exists('assets/wizard_small.bmp'):
    print('       Creating wizard small image...')
    w, h = 55, 55
    row_size = (w * 3 + 3) // 4 * 4
    img_size = row_size * h
    file_size = 54 + img_size
    header = struct.pack('<2sIHHI', b'BM', file_size, 0, 0, 54)
    info = struct.pack('<IiiHHIIiiII', 40, w, h, 1, 24, 0, img_size, 0, 0, 0, 0)
    rows = []
    cx, cy = w // 2, h // 2
    for y in range(h):
        row = b''
        for x in range(w):
            dist = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
            if dist < 20:
                row += bytes([96, 69, 233])
            else:
                row += bytes([46, 33, 26])
        row += b'\x00' * (row_size - w * 3)
        rows.append(row)
    with open('assets/wizard_small.bmp', 'wb') as f:
        f.write(header + info + b''.join(rows))
    print('       Wizard small image created')

print('       Assets ready')
