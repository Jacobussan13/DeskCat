EIGEN SPRITES PER KAT
=====================
Elke kat leest zijn sprites uit een eigen map naast catpet.py:

    sprites/cat1/   -> eerste kat
    sprites/cat2/   -> tweede kat
    sprites/cat3/   -> ...
    sprites/default/-> wordt gebruikt door katten zonder eigen map

Maak de mappen met:   python catpet.py --cats 3 --dump-sprites
Dat schrijft per kat alle animaties als PNG-strips (frames naast elkaar, 40x33 px per frame)
plus een config.json. Bewerk de PNG's in Aseprite / Piskel / GIMP en start catpet opnieuw.
Een PNG die ontbreekt valt terug op de ingebouwde animatie, dus je kunt ook maar een deel vervangen.
Sprites moeten naar RECHTS kijken (links wordt automatisch gespiegeld); bij 'stretch_wall' zit
de muur aan de rechterkant van het frame.

config.json per map:
  frame_width   breedte van een frame (standaard 40)
  mouth_x       x-positie van de mond in 'eat' (bepaalt waar de kat bij de bak staat)
  anims.<naam>  fps, tfps, intro, outro, durs (tijd per frame, speelt 1x), sfx ({"3": ["mew", 0.7]})

Animaties: walk hop idle sit lick_paw lick_tail wash sleep_loaf sleep_curl eat
           stretch_back stretch_wall purr  +  t_stand_sit t_sit_stand t_sit_lie t_lie_sit
Voerbak: sprites/bowl.png (4 frames: vol .. leeg) + bowl.json {"frame_width": 16}
Eigen geluid: sounds/purr.wav, mew.wav, crunch.wav naast catpet.py.
