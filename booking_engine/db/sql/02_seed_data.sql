-- Virtual Assistant Booking Engine — Seed Data (PostgreSQL)
-- Target: Neon PostgreSQL
-- Run after 01_schema.sql. Safe to re-run (ON CONFLICT DO NOTHING).

-- Shops
INSERT INTO shops (id, name, phone_number, address, welcome_message, tone_instructions, personality, special_instructions, is_active)
VALUES
  ('a0000000-0000-0000-0000-000000000001', 'Salon Bella', '+39 02 1234567', 'Via Roma 42, Milano',
   'Ciao, benvenuto da Salon Bella! Come ti chiami?',
   'Amichevole e informale, dai del tu al cliente',
   'Sei Bella, l''assistente virtuale del Salon Bella. Sei solare, cordiale e sempre pronta ad aiutare.',
   'Se il cliente chiede di un servizio che non offriamo, suggerisci il servizio più simile disponibile.',
   true),
  ('b0000000-0000-0000-0000-000000000002', 'Studio Hair', '+39 06 7654321', 'Via del Corso 15, Roma',
   'Buongiorno, benvenuto allo Studio Hair. Come posso aiutarla?',
   'Professionale e formale, dia del lei al cliente',
   'Sei l''assistente dello Studio Hair. Sei professionale, preciso e attento ai dettagli.',
   NULL,
   true)
ON CONFLICT (id) DO NOTHING;

-- Staff
INSERT INTO staff (id, shop_id, full_name, role, bio, is_active)
VALUES
  ('11111111-0000-0000-0000-000000000001', 'a0000000-0000-0000-0000-000000000001', 'Mirco Meazzo', 'stilista senior', 'Stilista con 15 anni di esperienza', true),
  ('11111111-0000-0000-0000-000000000002', 'a0000000-0000-0000-0000-000000000001', 'Giulia Verdi', 'colorista', 'Esperta di colorazioni e trattamenti', true),
  ('11111111-0000-0000-0000-000000000003', 'a0000000-0000-0000-0000-000000000001', 'Marco Bianchi', 'stilista', 'Specializzato in tagli classici e barba', true),
  ('22222222-0000-0000-0000-000000000001', 'b0000000-0000-0000-0000-000000000002', 'Anna Rossi', 'stilista senior', 'Stilista di fama internazionale', true),
  ('22222222-0000-0000-0000-000000000002', 'b0000000-0000-0000-0000-000000000002', 'Luca Neri', 'colorista', 'Esperto di balayage e meches', true)
ON CONFLICT (id) DO NOTHING;

-- Services
INSERT INTO services (id, shop_id, service_name, description, duration_minutes, price_eur, category, is_active)
VALUES
  ('aaaa0001-0000-0000-0000-000000000001', 'a0000000-0000-0000-0000-000000000001', 'Taglio donna', 'Taglio, shampoo e piega', 45, 35.00, 'taglio', true),
  ('aaaa0001-0000-0000-0000-000000000002', 'a0000000-0000-0000-0000-000000000001', 'Taglio uomo', 'Taglio maschile classico o moderno', 30, 25.00, 'taglio', true),
  ('aaaa0001-0000-0000-0000-000000000003', 'a0000000-0000-0000-0000-000000000001', 'Colore', 'Colorazione completa', 60, 55.00, 'colore', true),
  ('aaaa0001-0000-0000-0000-000000000004', 'a0000000-0000-0000-0000-000000000001', 'Meches', 'Meches o colpi di sole', 90, 70.00, 'colore', true),
  ('aaaa0001-0000-0000-0000-000000000005', 'a0000000-0000-0000-0000-000000000001', 'Piega', 'Piega semplice', 30, 20.00, 'piega', true),
  ('aaaa0001-0000-0000-0000-000000000006', 'a0000000-0000-0000-0000-000000000001', 'Trattamento cheratina', 'Trattamento lisciante alla cheratina', 120, 90.00, 'trattamento', true),
  ('bbbb0001-0000-0000-0000-000000000001', 'b0000000-0000-0000-0000-000000000002', 'Taglio donna', 'Taglio con consulenza personalizzata', 50, 45.00, 'taglio', true),
  ('bbbb0001-0000-0000-0000-000000000002', 'b0000000-0000-0000-0000-000000000002', 'Colore', 'Colorazione premium', 75, 80.00, 'colore', true),
  ('bbbb0001-0000-0000-0000-000000000003', 'b0000000-0000-0000-0000-000000000002', 'Balayage', 'Balayage naturale', 120, 120.00, 'colore', true),
  ('bbbb0001-0000-0000-0000-000000000004', 'b0000000-0000-0000-0000-000000000002', 'Piega', 'Piega professionale', 30, 30.00, 'piega', true)
ON CONFLICT (id) DO NOTHING;

-- Staff <-> Services
INSERT INTO staff_services (staff_id, service_id)
VALUES
  ('11111111-0000-0000-0000-000000000001', 'aaaa0001-0000-0000-0000-000000000001'),
  ('11111111-0000-0000-0000-000000000001', 'aaaa0001-0000-0000-0000-000000000002'),
  ('11111111-0000-0000-0000-000000000001', 'aaaa0001-0000-0000-0000-000000000005'),
  ('11111111-0000-0000-0000-000000000002', 'aaaa0001-0000-0000-0000-000000000003'),
  ('11111111-0000-0000-0000-000000000002', 'aaaa0001-0000-0000-0000-000000000004'),
  ('11111111-0000-0000-0000-000000000002', 'aaaa0001-0000-0000-0000-000000000005'),
  ('11111111-0000-0000-0000-000000000002', 'aaaa0001-0000-0000-0000-000000000006'),
  ('11111111-0000-0000-0000-000000000003', 'aaaa0001-0000-0000-0000-000000000001'),
  ('11111111-0000-0000-0000-000000000003', 'aaaa0001-0000-0000-0000-000000000002'),
  ('11111111-0000-0000-0000-000000000003', 'aaaa0001-0000-0000-0000-000000000005'),
  ('22222222-0000-0000-0000-000000000001', 'bbbb0001-0000-0000-0000-000000000001'),
  ('22222222-0000-0000-0000-000000000001', 'bbbb0001-0000-0000-0000-000000000002'),
  ('22222222-0000-0000-0000-000000000001', 'bbbb0001-0000-0000-0000-000000000003'),
  ('22222222-0000-0000-0000-000000000001', 'bbbb0001-0000-0000-0000-000000000004'),
  ('22222222-0000-0000-0000-000000000002', 'bbbb0001-0000-0000-0000-000000000002'),
  ('22222222-0000-0000-0000-000000000002', 'bbbb0001-0000-0000-0000-000000000003')
ON CONFLICT (staff_id, service_id) DO NOTHING;

-- Staff Schedules (all staff: Mon-Sat 10:00-18:00)
INSERT INTO staff_schedules (id, staff_id, day_of_week, start_time, end_time)
SELECT gen_random_uuid(), s.id, d.day, '10:00', '18:00'
FROM staff s
CROSS JOIN (VALUES (0),(1),(2),(3),(4),(5)) AS d(day)
ON CONFLICT (staff_id, day_of_week) DO NOTHING;

-- Sample Customers
INSERT INTO customers (id, shop_id, full_name, preferred_staff_id)
VALUES
  ('cccc0001-0000-0000-0000-000000000001', 'a0000000-0000-0000-0000-000000000001', 'Maria Rossi', '11111111-0000-0000-0000-000000000001'),
  ('cccc0001-0000-0000-0000-000000000002', 'a0000000-0000-0000-0000-000000000001', 'Luca Ferrari', NULL),
  ('cccc0002-0000-0000-0000-000000000001', 'b0000000-0000-0000-0000-000000000002', 'Francesca Bianchi', NULL)
ON CONFLICT (id) DO NOTHING;

-- Sample Phone Contacts
INSERT INTO phone_contacts (id, phone_number, customer_id)
VALUES
  (gen_random_uuid(), '+39 333 1111111', 'cccc0001-0000-0000-0000-000000000001'),
  (gen_random_uuid(), '+39 333 2222222', 'cccc0001-0000-0000-0000-000000000002'),
  (gen_random_uuid(), '+39 333 3333333', 'cccc0002-0000-0000-0000-000000000001')
ON CONFLICT (phone_number, customer_id) DO NOTHING;
