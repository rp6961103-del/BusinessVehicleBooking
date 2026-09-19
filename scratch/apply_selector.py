import os

files_to_update = [
    'templates/vehicle_details.html',
    'templates/booking_success.html',
    'templates/add_vehicle.html',
    'templates/my_vehicles.html',
    'templates/edit_vehicles.html',
    'templates/payment_checkout.html',
    'templates/payment_success.html',
    'templates/rate_booking.html',
    'templates/forgot_password.html',
]

snippet = '\n{% include " _language_selector.html\ %}\n'

for rel_path in files_to_update:
 if os.path.exists(rel_path):
 with open(rel_path, 'r', encoding='utf-8') as f:
 content = f.read()
 if '_language_selector.html' not in content:
 if '</header>' in content:
 content = content.replace('</header>', '</header>' + snippet, 1)
 elif '<body' in content:
 # insert right after body open tag
 pos = content.find('>')
 if pos != -1:
 body_idx = content.find('<body')
 if body_idx != -1:
 end_body_tag = content.find('>', body_idx)
 content = content[:end_body_tag+1] + snippet + content[end_body_tag+1:]
 with open(rel_path, 'w', encoding='utf-8') as f:
 f.write(content)
 print(f'Updated {rel_path}')
 else:
 print(f'Already has selector: {rel_path}')
