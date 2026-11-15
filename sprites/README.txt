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
  held_pivot    [x, y] punt waar de kat wordt vastgehouden als je hem optilt (standaard [21, 10])
  anims.<naam>  fps, tfps, intro, outro, durs (tijd per frame, speelt 1x), sfx ({"3": ["mew", 0.7]})

Animaties:
  lopen/staan   walk hop idle walk_low run stalk
  zitten/slapen sit lick_paw lick_tail wash sleep_loaf sleep_curl yawn stare
  tussenstappen t_stand_sit t_sit_stand t_sit_lie t_lie_sit
  rekken/eten   stretch_back stretch_wall eat clatter (rammelen aan de lege bak) purr
  stemmingen    happy angry sad
  samen         jump (over een andere kat; 'durs' = crouch, afzet, top, daling, landing, herstel;
                de boog zelf wordt door het programma bewogen)  invite roll catch hiss swat
  muis          pounce (zelfde opbouw als jump) wiggle reach stalk catch
  optillen      held (hangt aan de nekvel)  fly (gegooid, draait mee)  dizzy
Voerbak: sprites/bowl.png (4 frames: vol .. leeg) + bowl.json {"frame_width": 16}
Eigen geluid: sounds/<naam>.wav naast catpet.py, met naam: purr mew crunch hiss growl yowl chirp trill thud swat clank clink
