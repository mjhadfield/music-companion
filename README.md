# Music Companion

An interactive archive of my obsession with music, linking together my record collection, my listening history and every gig I've been to.

**See it live:** [mjhadfield.github.io/music-companion](https://mjhadfield.github.io/music-companion/) (works on mobile too)

## Background

Music has been a big part of my life for as long as I can remember, and I've always been a stats junkie. I wanted one place where I could pick a song and see everything about it: whether I own it on vinyl, how many times I've played it, and every time I've seen it live, with the full setlist from each show. Basically an interactive rabbit hole.

It pulls from three places:

- **Physical** - my vinyl collection, from Discogs
- **Digital** - around 100k scrobbles from Last.fm (only back to 2018, everything before that is lost to time)
- **Live** - every gig I've been to, with setlists from Setlist.fm

The hard part is that the data is a mess. "Remastered" vs "Deluxe Edition" vs "40th Anniversary Edition", live versions, singles that pretend to be albums, bands spelt three different ways... MusicBrainz IDs tie everything back together, so the same song from Discogs, Last.fm and Setlist.fm ends up as one row.

## How it works

It's all one SQLite database. The site is plain HTML/JS with no backend: it downloads a slimmed-down copy of the database and queries it in the browser with [sql.js](https://sql.js.org/), so it can live on GitHub Pages for free.

Behind that is a maintenance tool that runs at home. It pulls in new data, looks up missing IDs and finds duplicates, then puts everything in a list for me to review before anything is saved. I started out doing this by hand with SQL inserts, which was a complete nightmare.

## Can I run it?

Not really. It's built around my own data and hooked into my home setup ([Citadel](https://www.mikehadfield.co.uk/#/projects/citadel)), so it isn't designed to be picked up and run by anyone else. You're welcome to have a look at the code though.

There's a full write-up with screenshots on [my website](https://www.mikehadfield.co.uk/#/projects/music-companion).

## Credits

Data from [MusicBrainz](https://musicbrainz.org/), [Last.fm](https://www.last.fm/), [Setlist.fm](https://www.setlist.fm/), [Discogs](https://www.discogs.com/) and the [Cover Art Archive](https://coverartarchive.org/). Not affiliated with or endorsed by any of them.
